"""Screen 1 — Confirm proposal — structural and behavioural tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.confirm import (
    ConfirmContext,
    render,
    run,
)
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    Posting,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _console() -> Console:
    """Build a Console with the same flags the CLI uses for screens.

    `emoji=False` is critical: account paths like `Assets:B:SPK` would
    otherwise be rewritten by Rich's emoji shortcode subsystem.
    """
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _txn(amount: Decimal = Decimal("-12.50")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 4),
        amount=amount,
        currency="EUR",
        payee="STARBUCKS COFFEE GERMANY GMBH",
        description="Card payment 04.03.",
        bank_key="spk",
    )


def _proposal() -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Food"),),
        payee="Starbucks",
        narration="Coffee",
    )


def _rule() -> CategorizationRule:
    return CategorizationRule(
        target_account="Expenses:Food",
        payee_pattern="coffee|starbucks",
    )


def _ctx(**overrides) -> ConfirmContext:
    base = {
        "txn": _txn(),
        "proposal": _proposal(),
        "bank_account": "Assets:B:SPK",
        "kind": "auto_matched",
        "matched_rule": _rule(),
        "progress": (12, 47),
        "bank_key": "spk",
        "year": 2024,
    }
    base.update(overrides)
    return ConfirmContext(**base)  # type: ignore[arg-type]


# ── Structural assertions ─────────────────────────────────────────────────────


class TestRender:
    def test_header_includes_progress_and_bank(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert "[12/47]" in out
        assert "spk · 2024" in out

    def test_active_tag_renders_when_set(self):
        con = _console()
        render(con, _ctx(active_tag="italy-trip", tag_remaining=4))
        out = con.export_text()
        assert "tag: italy-trip" in out
        assert "4 left" in out

    def test_no_tag_label_when_unset(self):
        con = _console()
        render(con, _ctx())
        assert "no tag" in con.export_text()

    def test_glyph_is_pencil_for_decision_pending(self):
        con = _console()
        render(con, _ctx())
        # The header glyph signals "decision needed"; users orient on it.
        assert "✎" in con.export_text()

    def test_headline_shows_amount_and_account_arrow(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert "2024-03-04" in out
        assert "-12.50 EUR" in out
        # Glyph + space + account on both sides of the arrow.
        assert "◆ Assets:B:SPK" in out
        assert "↓ Expenses:Food" in out
        assert "→" in out

    def test_credit_amount_has_explicit_plus_sign(self):
        con = _console()
        render(con, _ctx(txn=_txn(Decimal("3000.00"))))
        # The user reading the ticker shouldn't have to infer sign;
        # explicit `+` matches negative `-` for symmetry.
        assert "+3000.00 EUR" in con.export_text()

    def test_auto_matched_shows_rule_provenance(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert "Matched rule" in out
        assert "/coffee|starbucks/" in out
        assert "in payee" in out

    def test_top_candidate_shows_reuse_provenance(self):
        existing = LedgerEntry(
            date=date(2024, 2, 28),
            narration="Old",
            source_account="Assets:B:SPK",
            target_account="Expenses:Food",
            amount=Decimal("-12.50"),
            currency="EUR",
        )
        con = _console()
        render(
            con,
            _ctx(
                kind="top_candidate",
                matched_rule=None,
                matched_entry=existing,
            ),
        )
        out = con.export_text()
        assert "Reusing target from existing entry" in out
        assert "2024-02-28" in out

    def test_fresh_pick_shows_similar_upcoming_hint_when_nonzero(self):
        con = _console()
        render(con, _ctx(kind="fresh_pick", matched_rule=None, similar_upcoming=3))
        out = con.export_text()
        assert "similar transactions upcoming" in out
        assert "3" in out

    def test_fresh_pick_hides_hint_when_zero(self):
        con = _console()
        render(
            con,
            _ctx(kind="fresh_pick", matched_rule=None, similar_upcoming=0),
        )
        out = con.export_text()
        assert "upcoming" not in out
        # And no rule provenance either — the screen just shows the proposal.
        assert "Matched rule" not in out

    def test_raw_block_shows_unedited_payee_and_description(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert "STARBUCKS COFFEE GERMANY GMBH" in out
        assert "Card payment 04.03." in out

    def test_will_write_block_shows_proposal_values(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert 'narration:    "Coffee"' in out
        assert 'payee:        "Starbucks"' in out
        assert "category:     ↓ Expenses:Food" in out

    def test_tag_line_only_when_proposal_has_tag(self):
        con1 = _console()
        render(
            con1,
            _ctx(
                proposal=_proposal().model_copy(update={"tag": "italy-trip"})
            ),
        )
        assert "#italy-trip" in con1.export_text()

        con2 = _console()
        render(con2, _ctx())
        assert "#" not in con2.export_text()

    def test_hotkey_row_shows_step2_choices(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        for hotkey in ("[enter] confirm", "[n] narration", "[p] payee", "[s] skip", "[q] quit"):
            assert hotkey in out, f"missing hotkey row entry: {hotkey}"

    def test_active_tag_without_remaining_omits_left_suffix(self):
        # `always`-mode tags don't expire; the `(N left)` suffix is only
        # for `duration`-mode tags. Header must distinguish.
        con = _console()
        render(con, _ctx(active_tag="italy-trip", tag_remaining=None))
        out = con.export_text()
        assert "tag: italy-trip" in out
        assert "left" not in out

    def test_empty_target_account_shows_question_mark(self):
        # Defensive: a malformed proposal (no postings) renders `?` rather
        # than raising. The user sees an obviously-broken state instead of
        # a crash mid-categorization.
        con = _console()
        empty_proposal = CategoryProposal(action="categorize", postings=())
        render(con, _ctx(proposal=empty_proposal))
        assert "→  ?" in con.export_text()


# ── Behavioural tests (run loop with monkeypatched Prompt.ask) ────────────────


def _scripted(*answers: str):
    """Return a Prompt.ask substitute that yields `answers` in order."""
    it = iter(answers)
    return lambda *a, **kw: next(it)


class TestRun:
    def test_enter_confirms_unchanged_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.target_account == "Expenses:Food"

    def test_skip_returns_skip_decision(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        decision = run(_console(), _ctx())
        assert decision.action == "skip"
        assert decision.proposal is None

    def test_quit_returns_quit_decision(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        decision = run(_console(), _ctx())
        assert decision.action == "quit"

    def test_n_edits_narration_then_enter_confirms(self, monkeypatch):
        # Sequence: hotkey "n", new narration "Coffee shop", hotkey "" (Enter).
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "Coffee shop near Vatican", ""),
        )
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.narration == "Coffee shop near Vatican"

    def test_p_edits_payee_then_enter_confirms(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("p", "Starbucks Roma", "")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.payee == "Starbucks Roma"

    def test_multiple_edits_compound(self, monkeypatch):
        # Edit narration, then payee, then confirm — both edits must persist.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "New narration", "p", "New payee", ""),
        )
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.narration == "New narration"
        assert decision.proposal.payee == "New payee"

    def test_t_opens_tag_menu_and_stamps_delta(self, monkeypatch):
        # `t` → tag menu mode `2` (always) → tag name "italy-trip" → enter.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("t", "2", "italy-trip", ""),
        )
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        delta = decision.proposal.tag_state_delta
        assert delta is not None
        assert delta.op == "set"
        assert delta.new_state is not None
        assert delta.new_state.tag == "italy-trip"

    def test_t_then_cancel_leaves_proposal_untouched(self, monkeypatch):
        # `t` → cancel (`5`) → enter. tag_state_delta stays None.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("t", "5", "")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.tag_state_delta is None

    def test_edit_then_skip_discards_edit(self, monkeypatch):
        # Skip after editing should not write — caller honours `action` only.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("n", "Edited", "s")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "skip"
        assert decision.proposal is None

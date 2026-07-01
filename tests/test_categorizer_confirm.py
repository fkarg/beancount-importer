"""Screen 1 — Confirm proposal — structural and behavioural tests."""

from __future__ import annotations

from dataclasses import replace
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
from beancount_importer.rules.tags import ActiveTag


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

    def test_will_write_highlights_changed_fields_against_matched_entry(self):
        # When the proposal differs from the matched entry, each
        # changed field gets a `(was: "...")` annotation so the user
        # sees exactly what the rule (or top-candidate reuse) is
        # changing. Unchanged fields don't show the annotation.
        from beancount_importer.models import LedgerEntry

        existing = LedgerEntry(
            date=date(2024, 3, 4),
            narration="Coffee",  # matches proposal narration → no diff
            source_account="Assets:B:SPK",
            target_account="Expenses:Drinks",  # differs → flagged
            amount=Decimal("-12.50"),
            currency="EUR",
            payee="Old Bucks",  # differs → flagged
        )
        con = _console()
        render(con, _ctx(matched_entry=existing))
        out = con.export_text()
        # Changed fields show `(was: ...)` callouts. Account fields
        # render with their glyph; text fields are quoted.
        assert '(was: "Old Bucks")' in out
        assert "(was: ↓ Expenses:Drinks)" in out
        # Unchanged narration shows no annotation — line is dimmed.
        narr_lines = [
            line for line in out.splitlines() if "narration:" in line
        ]
        assert len(narr_lines) == 1
        assert "(was:" not in narr_lines[0]

    def test_will_write_no_annotation_when_no_matched_entry(self):
        # Fresh-import path (no candidate to diff against) — the block
        # renders plain values without `(was:)` callouts on any field.
        con = _console()
        render(con, _ctx(matched_entry=None))
        out = con.export_text()
        assert "(was:" not in out

    def test_will_write_marks_field_as_new_when_old_was_empty(self):
        # Existing entry has no payee; the proposal fills one in. The
        # diff line shows `(new)` rather than `(was: "")` because there
        # was nothing there before.
        from beancount_importer.models import LedgerEntry

        existing = LedgerEntry(
            date=date(2024, 3, 4),
            narration="Coffee",
            source_account="Assets:B:SPK",
            target_account="Expenses:Food",
            amount=Decimal("-12.50"),
            currency="EUR",
            payee="",  # empty → proposal fills it
        )
        con = _console()
        render(con, _ctx(matched_entry=existing))
        out = con.export_text()
        assert "(new)" in out

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

    def test_will_write_previews_unstamped_active_tag_in_window(self):
        # The pipeline stamps `proposal.tag` only after this screen returns;
        # the preview must still show the active tag that *will* be written.
        con = _console()
        active = ActiveTag(
            tag="usa-2024", mode="duration", until_date=date(2024, 4, 11)
        )
        render(con, _ctx(current_active_tag=active, active_tag="usa-2024"))
        assert "#usa-2024" in con.export_text()

    def test_will_write_hides_active_tag_outside_window(self):
        # A duration tag whose window ends before the txn date will not be
        # stamped — the preview must not claim otherwise.
        con = _console()
        active = ActiveTag(
            tag="usa-2024", mode="duration", until_date=date(2024, 1, 1)
        )
        render(con, _ctx(current_active_tag=active, active_tag="usa-2024"))
        assert "#usa-2024" not in con.export_text()

    def test_hotkey_row_shows_step2_choices(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        for hotkey in (
            "[enter] confirm",
            "[n] narration",
            "[p] payee",
            "[c] change account",
            "[s] skip",
            "[q] quit",
        ):
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
        # `t` → new tag (`n`) → name "italy-trip" → mode `2` (always) → enter.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("t", "n", "italy-trip", "2", ""),
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
        # `t` → cancel (`.`) → enter. tag_state_delta stays None.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("t", ".", "")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.tag_state_delta is None

    def test_t_set_until_rerenders_with_pending_tag(self, monkeypatch):
        # `t` → new tag (`n`) → name "usa-2024" → until (`3`) → date inside
        # window → enter. The re-render after the menu must show the pending
        # tag in both the header and the "Will write" block.
        con = _console()
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("t", "n", "usa-2024", "3", "2024-04-11", ""),
        )
        decision = run(con, _ctx())
        out = con.export_text()
        assert decision.action == "confirm"
        assert "tag: usa-2024" in out  # header
        assert "#usa-2024" in out  # Will-write block

    def test_t_set_until_past_date_shows_no_tag_in_preview(self, monkeypatch):
        # A window ending before the txn date won't stamp; the re-render must
        # not show a tag the pipeline will never write.
        con = _console()
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("t", "n", "usa-2024", "3", "2024-01-01", ""),
        )
        decision = run(con, _ctx())
        assert decision.action == "confirm"
        assert "#usa-2024" not in con.export_text()

    def test_t_clear_removes_tag_from_preview(self, monkeypatch):
        # Start with an active tag, then `[t]` → clear (`c`). The preview's
        # tag line must disappear on the re-render.
        active = ActiveTag(tag="usa-2024", mode="always")
        con = _console()
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("t", "c", ""))
        decision = run(
            con, _ctx(current_active_tag=active, active_tag="usa-2024")
        )
        out = con.export_text()
        assert decision.action == "confirm"
        # Cleared: the post-clear render shows no tag line. (The pre-clear
        # render did, so we check the final state via the delta + that the
        # header's "no tag" appears after the menu.)
        assert decision.proposal is not None
        assert decision.proposal.tag_state_delta is not None
        assert decision.proposal.tag_state_delta.op == "clear"
        assert "no tag" in out

    def test_edit_then_skip_discards_edit(self, monkeypatch):
        # Skip after editing should not write — caller honours `action` only.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("n", "Edited", "s")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "skip"
        assert decision.proposal is None

    def test_r_edits_matched_rule_and_flags_replacement(self, monkeypatch):
        # A rule matched this txn → `[r]` edits it and marks it for replacement.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("r", "", ""))
        decision = run(_console(), _ctx())  # default matched_rule=_rule()
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.save_as_rule is True
        assert decision.proposal.pending_rule is not None
        assert decision.proposal.replaces_rule == _rule()

    def test_r_creates_new_rule_when_none_matched(self, monkeypatch):
        # No matched rule → `[r]` creates a fresh rule (nothing to replace).
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("r", "", ""))
        decision = run(_console(), _ctx(matched_rule=None))
        assert decision.proposal is not None
        assert decision.proposal.save_as_rule is True
        assert decision.proposal.pending_rule is not None
        assert decision.proposal.replaces_rule is None

    def test_r_editor_cancel_leaves_proposal_unsaved(self, monkeypatch):
        # `[r]` then `[.]` cancels the editor — save_as_rule stays off.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("r", ".", ""))
        decision = run(_console(), _ctx())
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.save_as_rule is False
        assert decision.proposal.pending_rule is None

    def test_save_as_rule_indicator_shows_edited_rule(self, monkeypatch):
        # After saving via the editor, the "Will write" block reflects the
        # pending rule's match field + mode. No matched rule → create path,
        # seeded contains-mode on the txn payee.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("r", "", ""))
        console = _console()
        run(console, _ctx(matched_rule=None))
        out = console.export_text()
        assert "save as rule:" in out
        assert "on payee contains:" in out

    def test_save_as_rule_indicator_falls_back_without_pending_rule(self):
        # A proposal that carries save_as_rule but no pending_rule (e.g. a
        # replayed decision) renders the derive-heuristic indicator.
        console = _console()
        ctx = _ctx()
        ctx = replace(
            ctx, proposal=ctx.proposal.model_copy(update={"save_as_rule": True})
        )
        render(console, ctx)
        out = console.export_text()
        assert "save as rule:" in out
        assert "on payee:" in out

    def test_c_returns_change_account_with_current_proposal(self, monkeypatch):
        # `[c]` is a transition action — Screen 1 hands back the in-flight
        # proposal so the host can preserve narration/payee edits across
        # the Screen 2 round-trip.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "Coffee Roma", "c"),
        )
        decision = run(_console(), _ctx())
        assert decision.action == "change_account"
        assert decision.proposal is not None
        # Edit-before-[c] preserved.
        assert decision.proposal.narration == "Coffee Roma"


# ── Near-miss diagnostic rendering ───────────────────────────────────────────


from beancount_importer.categorizer.confirm import render_near_misses
from beancount_importer.pipeline import NearMiss


def _miss_below() -> NearMiss:
    return NearMiss(
        entry=LedgerEntry(
            date=date(2024, 4, 2),
            narration="dim narration",
            source_account="Assets:B:SPK",
            target_account="Expenses:Fitness:Gym",
            amount=Decimal("-49.50"),
            currency="EUR",
        ),
        score=0.31,
        reason="below_threshold",
    )


def _miss_elsewhere() -> NearMiss:
    return NearMiss(
        entry=LedgerEntry(
            date=date(2024, 4, 2),
            narration="—",
            source_account="Assets:B:SPK:Checking",
            target_account="Expenses:Fitness:Gym",
            amount=Decimal("-49.50"),
            currency="EUR",
        ),
        score=1.0,
        reason="different_bucket",
    )


class TestNearMissRendering:
    def test_empty_renders_nothing(self):
        console = _console()
        render_near_misses(console, (), "Assets:B:SPK")
        assert console.export_text() == ""

    def test_below_threshold_renders_score_and_threshold_note(self):
        console = _console()
        render_near_misses(console, (_miss_below(),), "Assets:B:SPK")
        out = console.export_text()
        assert "Closest existing on" in out
        assert "Assets:B:SPK" in out
        assert "0.31" in out
        assert "below threshold" in out

    def test_different_bucket_names_the_other_account(self):
        console = _console()
        render_near_misses(console, (_miss_elsewhere(),), "Assets:B:SPK")
        out = console.export_text()
        assert "Same amount/date elsewhere" in out
        assert "Assets:B:SPK:Checking" in out
        assert "not in this bank's bucket" in out

    def test_both_render_in_order(self):
        console = _console()
        render_near_misses(
            console,
            (_miss_below(), _miss_elsewhere()),
            "Assets:B:SPK",
        )
        out = console.export_text()
        # below_threshold line precedes different_bucket line
        assert out.index("Closest existing") < out.index("Same amount/date")

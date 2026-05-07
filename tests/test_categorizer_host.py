"""Screen-driven categorizer host — integration tests.

Verifies that `make_screen_categorizer` routes a `CategorizeContext` to
the right screens and translates each screen's decision back into a
`CategoryProposal` the pipeline can consume.

The screen modules are unit-tested independently; here we focus on
routing and the proposal-shape mapping at the seam.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.host import make_screen_categorizer
from beancount_importer.models import (
    LedgerEntry,
    SourceTransaction,
)
from beancount_importer.pipeline import CategorizeContext
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _txn(amount: Decimal = Decimal("-12.50")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 4),
        amount=amount,
        currency="EUR",
        payee="Starbucks",
        description="Coffee",
        bank_key="spk",
    )


def _entry(target: str, amount: Decimal = Decimal("-12.50")) -> LedgerEntry:
    return LedgerEntry(
        date=date(2024, 3, 4),
        narration="Coffee",
        source_account="Assets:B:SPK",
        target_account=target,
        amount=amount,
        currency="EUR",
    )


def _ctx(**overrides) -> CategorizeContext:
    base = {
        "txn": _txn(),
        "rules": (),
        "candidates": (),
        "matched_rule": None,
        "account_hints": (),
        "active_tag": None,
        "existing_entries": (),
        "source_account": "Assets:B:SPK",
        "progress": (1, 10),
    }
    base.update(overrides)
    return CategorizeContext(**base)  # type: ignore[arg-type]


def _scripted(*answers: str):
    it = iter(answers)
    return lambda *a, **kw: next(it)


# ── Path A: rule matched → Screen 1 ───────────────────────────────────────────


class TestRuleMatched:
    def test_enter_returns_rule_target_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        rule = CategorizationRule(
            target_account="Expenses:Food",
            payee_pattern="starbucks",
            override_payee="Starbucks",
            override_narration="Coffee",
        )
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(matched_rule=rule))
        assert proposal.action == "categorize"
        assert proposal.target_account == "Expenses:Food"
        # Rule-driven proposal carries `rule_used` so the pipeline can
        # apply transforms and build a derived rule if asked.
        assert proposal.rule_used is rule

    def test_skip_returns_skip_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        rule = CategorizationRule(target_account="Expenses:Food")
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(matched_rule=rule))
        assert proposal.action == "skip"

    def test_quit_returns_quit_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        rule = CategorizationRule(target_account="Expenses:Food")
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(matched_rule=rule))
        assert proposal.action == "quit"

    def test_edit_narration_then_enter_keeps_edit(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "Coffee shop", ""),
        )
        rule = CategorizationRule(target_account="Expenses:Food")
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(matched_rule=rule))
        assert proposal.narration == "Coffee shop"


# ── Path A: top candidate → Screen 1 (top_candidate kind) ─────────────────────


class TestTopCandidate:
    def test_uses_candidate_target_when_no_rule(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        candidate = _entry("Expenses:Food")
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(candidates=((candidate, 0.9),)))
        assert proposal.action == "categorize"
        assert proposal.target_account == "Expenses:Food"
        # No rule attached on this path.
        assert proposal.rule_used is None

    def test_rule_wins_over_candidate(self, monkeypatch):
        # Both rule and candidate present → rule's target wins.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        candidate = _entry("Expenses:Wrong")
        rule = CategorizationRule(target_account="Expenses:Right")
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(
            _ctx(matched_rule=rule, candidates=((candidate, 0.9),))
        )
        assert proposal.target_account == "Expenses:Right"


# ── Path B: no rule, no candidate → Screen 2 → Screen 1 ───────────────────────


class TestPickThenConfirm:
    def test_pick_then_confirm_flow(self, monkeypatch):
        # Screen 2 picks #1 (Expenses:Food); Screen 1 confirms with Enter.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "")
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(existing_entries=existing))
        assert proposal.action == "categorize"
        assert proposal.target_account == "Expenses:Food"

    def test_pick_skip_short_circuits_screen_1(self, monkeypatch):
        # Screen 2 skip → no Screen 1 prompt at all.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("s")
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(existing_entries=existing))
        assert proposal.action == "skip"

    def test_pick_quit_short_circuits_screen_1(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("q")
        )
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx())
        assert proposal.action == "quit"

    def test_confirm_skip_after_pick_returns_skip(self, monkeypatch):
        # User picks an account on Screen 2 then changes mind on Screen 1.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "s")
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(_ctx(existing_entries=existing))
        assert proposal.action == "skip"

    def test_pick_kind_is_fresh_on_screen_1(self, monkeypatch):
        # Indirectly: a fresh-pick context has no matched rule provenance,
        # so the rendered output omits "Matched rule".
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "")
        )
        console = _console()
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(console)
        categorizer(_ctx(existing_entries=existing))
        out = console.export_text()
        assert "Matched rule" not in out


# ── Screen 1 [c] change account → Screen 2 → Screen 1 ────────────────────────


class TestChangeAccount:
    def test_c_round_trip_replaces_target_account(self, monkeypatch):
        # Auto-matched rule → Screen 1 → user presses [c] → Screen 2 → user
        # picks #1 (Expenses:Food) → Screen 1 with the new target → Enter.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("c", "1", "")
        )
        rule = CategorizationRule(
            payee_pattern="Starbucks", target_account="Expenses:Coffee"
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(
            _ctx(matched_rule=rule, existing_entries=existing)
        )
        assert proposal.action == "categorize"
        # User overrode the rule's target.
        assert proposal.target_account == "Expenses:Food"

    def test_c_preserves_narration_and_payee_edits(self, monkeypatch):
        # Edit narration + payee on Screen 1, press [c], pick a new
        # account on Screen 2 — edits must survive the round-trip.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted(
                "n", "Coffee Roma",
                "p", "Starbucks Italia",
                "c", "1", "",
            ),
        )
        rule = CategorizationRule(
            payee_pattern="Starbucks", target_account="Expenses:Coffee"
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(
            _ctx(matched_rule=rule, existing_entries=existing)
        )
        assert proposal.narration == "Coffee Roma"
        assert proposal.payee == "Starbucks Italia"
        assert proposal.target_account == "Expenses:Food"

    def test_c_then_pick_skip_short_circuits(self, monkeypatch):
        # User opens Screen 2 with [c], then bails with `s` — the whole
        # categorize call returns skip (caller sees "user backed out").
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("c", "s")
        )
        rule = CategorizationRule(
            payee_pattern="Starbucks", target_account="Expenses:Coffee"
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(
            _ctx(matched_rule=rule, existing_entries=existing)
        )
        assert proposal.action == "skip"

    def test_c_then_pick_quit_short_circuits(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("c", "q")
        )
        rule = CategorizationRule(
            payee_pattern="Starbucks", target_account="Expenses:Coffee"
        )
        existing = (_entry("Expenses:Food"),)
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(
            _ctx(matched_rule=rule, existing_entries=existing)
        )
        assert proposal.action == "quit"

    def test_c_then_l_opens_full_column_grid(self, monkeypatch):
        # End-to-end: [c] on Screen 1, [l] on Screen 2 → column grid,
        # numeric pick, then [enter] confirm. Verifies the column grid
        # is reachable from inside a [c] round-trip.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("c", "l", "1", ""),
        )
        rule = CategorizationRule(
            payee_pattern="Starbucks", target_account="Expenses:Coffee"
        )
        # Multiple existing entries → all_accounts pool is populated.
        existing = (
            _entry("Expenses:Food"),
            _entry("Expenses:Travel"),
            _entry("Expenses:Coffee"),
        )
        console = _console()
        categorizer = make_screen_categorizer(console)
        proposal = categorizer(
            _ctx(matched_rule=rule, existing_entries=existing)
        )
        assert proposal.action == "categorize"
        # Picked the alphabetically-first account from the column grid.
        out = console.export_text()
        # Column grid uses the [N] numeric label format.
        assert "[1]" in out


# ── Tag-state plumbing ────────────────────────────────────────────────────────


class TestActiveTag:
    def test_active_tag_passed_to_screens(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        active = ActiveTag(tag="italy-trip", mode="always")
        rule = CategorizationRule(target_account="Expenses:Food")
        console = _console()
        categorizer = make_screen_categorizer(console)
        categorizer(_ctx(matched_rule=rule, active_tag=active))
        # Header must include the active tag label.
        assert "tag: italy-trip" in console.export_text()

    def test_duration_tag_shows_remaining_days(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        active = ActiveTag(
            tag="italy-trip",
            mode="duration",
            from_date=date(2024, 3, 1),
            until_date=date(2024, 3, 8),
        )
        rule = CategorizationRule(target_account="Expenses:Food")
        console = _console()
        categorizer = make_screen_categorizer(console)
        # txn date is 2024-03-04 → 4 days remaining (until 03-08 inclusive).
        categorizer(_ctx(matched_rule=rule, active_tag=active))
        out = console.export_text()
        assert "4 left" in out

    def test_always_tag_omits_remaining_days(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        active = ActiveTag(tag="italy-trip", mode="always")
        rule = CategorizationRule(target_account="Expenses:Food")
        console = _console()
        categorizer = make_screen_categorizer(console)
        categorizer(_ctx(matched_rule=rule, active_tag=active))
        out = console.export_text()
        assert "tag: italy-trip" in out
        assert "left" not in out

    def test_duration_past_until_date_clamps_to_zero(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        active = ActiveTag(
            tag="italy-trip",
            mode="duration",
            until_date=date(2024, 1, 1),  # well before the txn date
        )
        rule = CategorizationRule(target_account="Expenses:Food")
        console = _console()
        categorizer = make_screen_categorizer(console)
        # Negative remaining must clamp; otherwise a stale tag prints `-N`.
        categorizer(_ctx(matched_rule=rule, active_tag=active))
        assert "0 left" in console.export_text()

    def test_duration_without_until_date_omits_remaining(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        active = ActiveTag(tag="t", mode="duration")
        rule = CategorizationRule(target_account="Expenses:Food")
        console = _console()
        categorizer = make_screen_categorizer(console)
        categorizer(_ctx(matched_rule=rule, active_tag=active))
        assert "left" not in console.export_text()


# ── Header progress threading ─────────────────────────────────────────────────


def test_existing_entries_with_blank_accounts_dont_pollute_counts(monkeypatch):
    """Synthesized virtual entries can carry blank source_account or
    target_account; those must not contribute "" to Screen 2's count map.
    """
    monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("1", ""))
    blank_target = LedgerEntry(
        date=date(2024, 1, 1),
        narration="x",
        source_account="Assets:B:SPK",
        target_account="",  # synthesized half
        amount=Decimal("-10"),
        currency="EUR",
    )
    blank_source = LedgerEntry(
        date=date(2024, 1, 2),
        narration="y",
        source_account="",
        target_account="Expenses:Misc",
        amount=Decimal("-10"),
        currency="EUR",
    )
    real = _entry("Expenses:Misc")
    categorizer = make_screen_categorizer(_console())
    proposal = categorizer(
        _ctx(existing_entries=(blank_target, blank_source, real))
    )
    # Picks #1 from suggestions; accounts must not include `""`.
    assert proposal.action == "categorize"
    assert proposal.target_account
    assert proposal.target_account != ""


# ── Path A0: ambiguous → Screen 4 ────────────────────────────────────────────


class TestAmbiguous:
    def _amb_ctx(self) -> CategorizeContext:
        # Two candidates within the default min_delta=0.15 — clearly
        # ambiguous. Different target accounts so the picked one is
        # observable in the resulting proposal.
        c1 = LedgerEntry(
            date=date(2024, 3, 1),
            narration="a",
            source_account="Assets:B:SPK",
            target_account="Expenses:Food",
            amount=Decimal("-12.50"),
            currency="EUR",
        )
        c2 = LedgerEntry(
            date=date(2024, 3, 2),
            narration="b",
            source_account="Assets:B:SPK",
            target_account="Expenses:Online",
            amount=Decimal("-12.50"),
            currency="EUR",
        )
        return _ctx(candidates=((c1, 0.90), (c2, 0.88)))

    def test_enter_picks_top_candidate_on_screen_4(self, monkeypatch):
        # Screen 4 enter == pick #1; Screen 1 enter confirms.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("", ""))
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(self._amb_ctx())
        assert proposal.action == "categorize"
        assert proposal.target_account == "Expenses:Food"

    def test_pick_two_routes_to_second_candidate(self, monkeypatch):
        # `2` on Screen 4 → Screen 1 confirms with that entry's target.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("2", ""))
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(self._amb_ctx())
        assert proposal.target_account == "Expenses:Online"

    def test_import_new_falls_through_to_pick_then_confirm(self, monkeypatch):
        # `i` on Screen 4 → Screen 2 → Screen 1 (fresh_pick).
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("i", "1", "")
        )
        ctx = self._amb_ctx()
        # Existing entries provide the suggestion list for Screen 2.
        ctx_with_existing = _ctx(
            candidates=ctx.candidates,
            existing_entries=(_entry("Expenses:Misc"),),
        )
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(ctx_with_existing)
        assert proposal.action == "categorize"
        assert proposal.target_account == "Expenses:Misc"

    def test_skip_returns_skip_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(self._amb_ctx())
        assert proposal.action == "skip"

    def test_quit_returns_quit_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        categorizer = make_screen_categorizer(_console())
        proposal = categorizer(self._amb_ctx())
        assert proposal.action == "quit"

    def test_wide_gap_is_not_ambiguous(self, monkeypatch):
        # 0.90 vs 0.40 → delta 0.50 > min_delta=0.15 → no Screen 4.
        # Goes straight to Screen 1 (top_candidate); Enter confirms.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        c1 = _entry("Expenses:Food")
        c2 = LedgerEntry(
            date=date(2024, 3, 2),
            narration="b",
            source_account="Assets:B:SPK",
            target_account="Expenses:Other",
            amount=Decimal("-12.50"),
            currency="EUR",
        )
        console = _console()
        categorizer = make_screen_categorizer(console)
        proposal = categorizer(_ctx(candidates=((c1, 0.90), (c2, 0.40))))
        assert proposal.target_account == "Expenses:Food"
        # Output should NOT mention multiple-candidate language.
        out = console.export_text()
        assert "Multiple ledger entries" not in out

    def test_rule_pre_empts_ambiguity_check(self, monkeypatch):
        # Even with two near-tied candidates, a matched rule wins —
        # Screen 4 is skipped, Screen 1 confirms with the rule's target.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        rule = CategorizationRule(target_account="Expenses:Rule")
        ctx = self._amb_ctx()
        ctx_with_rule = _ctx(
            matched_rule=rule, candidates=ctx.candidates
        )
        console = _console()
        categorizer = make_screen_categorizer(console)
        proposal = categorizer(ctx_with_rule)
        assert proposal.target_account == "Expenses:Rule"
        assert "Multiple ledger entries" not in console.export_text()

    def test_min_delta_param_lowers_ambiguity_bar(self, monkeypatch):
        # With min_delta=0.05, the same 0.90/0.88 candidates still trip
        # the ambiguity check (delta 0.02 < 0.05). Confirms the param
        # actually flows.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("", ""))
        console = _console()
        categorizer = make_screen_categorizer(console, min_delta=0.05)
        categorizer(self._amb_ctx())
        assert "Multiple ledger entries" in console.export_text()

    def test_min_delta_zero_disables_screen_4(self, monkeypatch):
        # `min_delta=0.0` means "only show ambiguous when scores are
        # exactly equal" — the 0.90/0.88 case slips through to Screen 1.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        console = _console()
        categorizer = make_screen_categorizer(console, min_delta=0.0)
        categorizer(self._amb_ctx())
        assert "Multiple ledger entries" not in console.export_text()


def test_progress_threaded_to_screens(monkeypatch):
    monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
    rule = CategorizationRule(target_account="Expenses:Food")
    console = _console()
    categorizer = make_screen_categorizer(console)
    categorizer(_ctx(matched_rule=rule, progress=(7, 23)))
    assert "[7/23]" in console.export_text()

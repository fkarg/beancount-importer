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


def test_progress_threaded_to_screens(monkeypatch):
    monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
    rule = CategorizationRule(target_account="Expenses:Food")
    console = _console()
    categorizer = make_screen_categorizer(console)
    categorizer(_ctx(matched_rule=rule, progress=(7, 23)))
    assert "[7/23]" in console.export_text()

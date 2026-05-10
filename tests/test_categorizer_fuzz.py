"""Fuzz the categorizer's UI state machine.

Each screen's `run` function is a small reactive loop: render → prompt →
handle → loop or return. The hand-written tests cover individual happy
paths, but a class of bugs lives between them — e.g. a hotkey listed in
the render but missing from the run-loop's match arms, or a silent
cancel-on-Enter that slips past code review.

These tests script `rich.prompt.Prompt.ask` with a randomised responder
that always returns a valid element of `choices` (or a small alphabet
for free-text prompts). For each seed, we run the screen and assert it
terminates with a typed decision. The seed sweep is wide enough that any
unhandled key or pathological loop surfaces as either a crash or an
infinite-call timeout (capped via the responder's call counter).

We use `pytest.mark.parametrize` rather than `hypothesis` here: the
input space is one opaque integer fed to `random.Random`, which
hypothesis can neither constrain nor shrink — so we'd pay its setup
cost without getting any of its benefits. A fixed seed list keeps runs
reproducible across machines and produces the same coverage in a
fraction of the wall time. Hypothesis is still used in `test_parsers.py`
where it actually explores structured inputs.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from beancount_importer.categorizer.ambiguous import (
    AmbiguousContext,
    AmbiguousDecision,
    run as run_ambiguous,
)
from beancount_importer.categorizer.collision import (
    CollisionContext,
    CollisionDecision,
    run as run_collision,
)
from beancount_importer.categorizer.confirm import (
    ConfirmContext,
    ConfirmDecision,
    run as run_confirm,
)
from beancount_importer.categorizer.modes.amortize import (
    run as run_amortize,
)
from beancount_importer.categorizer.pick import (
    PickContext,
    PickDecision,
    run as run_pick,
)
from beancount_importer.categorizer.tag_menu import run as run_tag_menu
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule


# ── Test plumbing ─────────────────────────────────────────────────────────────


def _console() -> Console:
    # `record=False`: the fuzz tests don't introspect rendered output —
    # only assert on the final decision shape — so the recorded-output
    # buffer would just be paid-for-and-thrown-away work per render.
    return Console(file=StringIO(), record=False, width=120, emoji=False)


# Ceiling on Prompt.ask invocations per run. If any screen's run loop
# spins past this many prompts, that's an infinite loop or pathological
# state — fail the test loudly. Real usage tops out at maybe ~10 prompts
# per transaction; 500 leaves plenty of room for legitimate edit→edit
# loops without hiding bugs.
_MAX_PROMPTS = 500


class _RandomResponder:
    """Stand-in for `Prompt.ask` that picks a random valid response.

    When `choices` is given, samples uniformly from it (the screen's
    own contract guarantees this is a safe answer). When the prompt is
    free-text, samples from a small alphabet that exercises both
    "valid-looking" and "invalid" paths (so callers' validation /
    re-prompt branches get hit).

    Tracks call count and forces termination once `_MAX_PROMPTS` is
    reached so a buggy run loop doesn't hang the test process.
    """

    # Free-text prompt fodder. Includes empty (cancel by convention),
    # numeric strings (account / month indices), text (custom names,
    # tags, payees, narrations), an ISO date (until_date prompt), and
    # garbage (drives the warning + reprompt branches).
    _FREETEXT = (
        "", "x", "abc", "1", "12", "999", "Expenses:Food",
        "tag_name", "2024-12-31", "twelve", "0",
    )

    # Preferred terminating choices, by priority. When we hit the cap
    # we pick the first one present in `choices`.
    _TERMINATORS = ("q", "s", "x", "5", "4")

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        choices = kwargs.get("choices")
        default = kwargs.get("default", "")
        if self.calls > _MAX_PROMPTS:
            if choices:
                for term in self._TERMINATORS:
                    if term in choices:
                        return term
                return list(choices)[0]
            return "x"
        if choices:
            return self.rng.choice(list(choices))
        # Free-text prompt — favour empty (cancel) more often than
        # garbage so screens that chain free-text prompts terminate.
        if self.rng.random() < 0.4:
            return default if default is not None else ""
        return self.rng.choice(self._FREETEXT)


def _patched_prompt(monkeypatch, seed: int) -> _RandomResponder:
    """Install the random responder and return it for assertions."""
    responder = _RandomResponder(seed)
    monkeypatch.setattr("rich.prompt.Prompt.ask", responder)
    return responder


# ── Fixture builders ──────────────────────────────────────────────────────────


def _txn(amount: Decimal = Decimal("-50.00")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 1),
        amount=amount,
        currency="EUR",
        payee="Vendor",
        description="d",
        bank_key="spk",
    )


def _proposal() -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Software"),),
    )


def _confirm_ctx(*, debit: bool = True) -> ConfirmContext:
    return ConfirmContext(
        txn=_txn(amount=Decimal("-50") if debit else Decimal("50")),
        proposal=_proposal(),
        bank_account="Assets:B:SPK",
        kind="auto_matched",
        matched_rule=CategorizationRule(target_account="Expenses:Software"),
    )


def _pick_ctx() -> PickContext:
    suggestions = (
        "Expenses:Online",
        "Expenses:Household",
        "Expenses:Electronics",
        "Expenses:Books",
        "Expenses:Unknown",
    )
    return PickContext(
        txn=_txn(),
        bank_account="Assets:B:SPK",
        suggestions=suggestions,
        suggestion_counts={"Expenses:Online": 5},
        all_accounts=suggestions + ("Expenses:Travel", "Expenses:Software"),
        existing_entries=(
            LedgerEntry(
                date=date(2024, 1, 1),
                narration="x",
                source_account="Assets:B:SPK",
                target_account="Assets:B:N26",
                amount=Decimal("100"),
            ),
        ),
    )


def _collision_ctx() -> CollisionContext:
    existing = LedgerEntry(
        date=date(2024, 3, 1),
        payee="Vendor",
        narration="old",
        source_account="Assets:B:SPK",
        target_account="Expenses:Software",
        amount=Decimal("-50"),
    )
    return CollisionContext(
        txn=_txn(),
        existing=existing,
        proposed_changes=[
            ProposedChange(field="narration", old_val="old", new_val="new"),
        ],
        proposal=_proposal(),
    )


def _ambiguous_ctx() -> AmbiguousContext:
    candidates = tuple(
        (
            LedgerEntry(
                date=date(2024, 3, 1),
                narration=f"cand {i}",
                source_account="Assets:B:SPK",
                target_account="Expenses:Software",
                amount=Decimal("-50"),
            ),
            1.5 - 0.1 * i,
        )
        for i in range(3)
    )
    return AmbiguousContext(txn=_txn(), candidates=candidates)


# ── The fuzz tests ────────────────────────────────────────────────────────────


# Fixed seed grid. 40 picks is enough to surface any unhandled-key or
# pathological-loop bug — same budget the previous `max_examples=40`
# used. Kept as `range(40)` rather than a hand-curated list so the
# coverage scales transparently if the fuzz space ever grows.
_SEEDS = list(range(40))


@pytest.mark.parametrize("seed", _SEEDS)
def test_confirm_run_always_terminates_with_typed_decision(
    seed: int, monkeypatch
):
    responder = _patched_prompt(monkeypatch, seed)
    decision = run_confirm(_console(), _confirm_ctx())
    assert isinstance(decision, ConfirmDecision)
    assert decision.action in {"confirm", "skip", "quit", "change_account"}
    assert responder.calls <= _MAX_PROMPTS


# 20 seeds × 2 polarities = 40 cases — same total fuzz budget as the
# other tests, with both debit and credit guaranteed to be exercised.
@pytest.mark.parametrize("debit", [True, False])
@pytest.mark.parametrize("seed", _SEEDS[:20])
def test_confirm_run_handles_both_debit_and_credit(
    seed: int, debit: bool, monkeypatch
):
    """The `[m]` hotkey is debit-only — fuzz both polarities to make
    sure the credit path doesn't accept a non-listed key.
    """
    _patched_prompt(monkeypatch, seed)
    decision = run_confirm(_console(), _confirm_ctx(debit=debit))
    assert isinstance(decision, ConfirmDecision)


@pytest.mark.parametrize("seed", _SEEDS)
def test_pick_run_always_terminates_with_typed_decision(
    seed: int, monkeypatch
):
    responder = _patched_prompt(monkeypatch, seed)
    decision = run_pick(_console(), _pick_ctx())
    assert isinstance(decision, PickDecision)
    assert decision.action in {"pick", "skip", "quit"}
    assert responder.calls <= _MAX_PROMPTS
    if decision.action == "pick":
        # Whatever the user "typed", the result must be a valid account.
        assert decision.account is not None


@pytest.mark.parametrize("seed", _SEEDS)
def test_collision_run_always_terminates_with_typed_decision(
    seed: int, monkeypatch
):
    _patched_prompt(monkeypatch, seed)
    decision = run_collision(_console(), _collision_ctx())
    assert isinstance(decision, CollisionDecision)
    assert decision.action in {"update", "keep", "import_new", "block", "skip", "quit"}


@pytest.mark.parametrize("seed", _SEEDS)
def test_ambiguous_run_always_terminates_with_typed_decision(
    seed: int, monkeypatch
):
    _patched_prompt(monkeypatch, seed)
    decision = run_ambiguous(_console(), _ambiguous_ctx())
    assert isinstance(decision, AmbiguousDecision)
    assert decision.action in {"pick", "import_new", "skip", "quit"}


@pytest.mark.parametrize("seed", _SEEDS)
def test_tag_menu_run_always_terminates(seed: int, monkeypatch):
    _patched_prompt(monkeypatch, seed)
    result = run_tag_menu(_console(), None)
    # Either a TagStateDelta or None — both are documented returns.
    assert result is None or hasattr(result, "op")


@pytest.mark.parametrize("seed", _SEEDS)
def test_amortize_run_always_returns_proposal(seed: int, monkeypatch):
    _patched_prompt(monkeypatch, seed)
    result = run_amortize(_console(), _proposal())
    assert isinstance(result, CategoryProposal)


# ── Targeted Enter-semantics tests ────────────────────────────────────────────


class TestEnterIsNeverASilentCancel:
    """The on-call bug that motivated the rewrite: every screen that
    advertises `[enter]` in its hotkey row must do something *visible*
    on Enter — confirm, advance, redraw — never silently dump back to
    the parent screen with no feedback.
    """

    def test_confirm_enter_is_confirm_not_skip(self, monkeypatch):
        # Screen 1 advertises `[enter] confirm`. Empty input must return
        # a confirm decision, not a skip / quit.
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "")
        decision = run_confirm(_console(), _confirm_ctx())
        assert decision.action == "confirm"

    def test_collision_enter_is_update(self, monkeypatch):
        # Screen 3 advertises `[enter] update`. Empty must update.
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "")
        decision = run_collision(_console(), _collision_ctx())
        assert decision.action == "update"

    def test_ambiguous_enter_is_pick_top(self, monkeypatch):
        # Screen 4 advertises `[enter] pick #1`. Empty picks the highest.
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "")
        decision = run_ambiguous(_console(), _ambiguous_ctx())
        assert decision.action == "pick"
        assert decision.entry is _ambiguous_ctx().candidates[0][0] or (
            decision.entry is not None
            and decision.entry.narration == "cand 0"
        )

    def test_tag_menu_does_not_advertise_enter(self):
        # Tag menu has no `[enter]` action — the menu has no obvious
        # default. The previous bug was `default="5"` which made Enter
        # silently cancel. Verify the run-loop's choices set excludes
        # the empty string, so Rich loops on Enter until a valid digit
        # is typed (visible to the user) instead of silently bailing.
        from beancount_importer.categorizer import tag_menu

        assert "" not in tag_menu._HOTKEYS

    def test_amortize_does_not_advertise_enter(self):
        # Same contract: amortize has no Enter shortcut, so Rich's
        # choices loop is what protects the user from a silent cancel.
        from beancount_importer.categorizer.modes import amortize

        assert "" not in amortize._HOTKEYS

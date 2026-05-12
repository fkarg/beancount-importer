"""Scripted test doubles for the interactive run path.

`ScriptedCategorizer` and `ScriptedMergeFn` let a test pre-record the
user choices a row-by-row interactive session would have produced, then
replay them through the real pipeline. Use them to exercise the full
CLI → pipeline → persistence wiring without driving a Rich prompt.

The scripts are keyed by `(payee, abs(amount))` — enough to disambiguate
the rows in any test fixture without coupling to booking-date or csv
position. A missing key raises rather than falling back: tests should
be explicit about every row they expect to process so a regression that
slips a new row through the pipeline fails loudly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import (
    CategorizeContext,
    CategorizeFn,
    MergeContext,
    MergeDecision,
    MergeFn,
)


def _key(payee: str | None, amount: Decimal) -> tuple[str, Decimal]:
    return (payee or "", abs(amount))


class ScriptedCategorizer:
    """A `CategorizeFn` that hands out pre-recorded proposals.

    Each call records the `CategorizeContext` it received on `self.calls`,
    so tests can assert on what the pipeline actually surfaced (rule
    matches, candidates, near-misses) in addition to the final result.
    """

    def __init__(self, script: dict[tuple[str, Decimal], CategoryProposal]):
        self.script = dict(script)
        self.calls: list[CategorizeContext] = []

    def __call__(self, ctx: CategorizeContext) -> CategoryProposal:
        self.calls.append(ctx)
        key = _key(ctx.txn.payee, ctx.txn.amount)
        if key not in self.script:
            raise AssertionError(
                f"ScriptedCategorizer: no proposal scripted for {key!r}. "
                f"Scripted keys: {sorted(self.script.keys())}"
            )
        return self.script[key]

    def as_fn(self) -> CategorizeFn:
        return self


def categorize_as(
    account: str,
    *,
    payee: str | None = None,
    narration: str | None = None,
    save_as_rule: bool = False,
    **extra: Any,
) -> CategoryProposal:
    """Convenience builder for a single categorize proposal entry."""
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=account),),
        payee=payee,
        narration=narration,
        save_as_rule=save_as_rule,
        **extra,
    )


def skip_proposal() -> CategoryProposal:
    return CategoryProposal(action="skip", postings=())


def quit_proposal() -> CategoryProposal:
    return CategoryProposal(action="quit", postings=())


class ScriptedMergeFn:
    """A `MergeFn` that returns pre-recorded merge decisions per row.

    Unlike `ScriptedCategorizer`, a missing key here defaults to `update`
    (the "keep the auto-generated diff" branch) so tests that don't
    care about merge prompts can still let updates flow through. Pass
    an explicit `default=` to override.
    """

    def __init__(
        self,
        script: dict[tuple[str, Decimal], MergeDecision] | None = None,
        *,
        default: MergeDecision | None = None,
    ):
        self.script = dict(script or {})
        self.default = default or MergeDecision(action="update")
        self.calls: list[MergeContext] = []

    def __call__(self, ctx: MergeContext) -> MergeDecision:
        self.calls.append(ctx)
        return self.script.get(_key(ctx.txn.payee, ctx.txn.amount), self.default)

    def as_fn(self) -> MergeFn:
        return self

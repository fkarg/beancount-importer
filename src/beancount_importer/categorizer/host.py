"""Screen-driven `CategorizeFn` host.

Bridges the pipeline's `CategorizeContext → CategoryProposal` slot to
the new screen-based prompt layer (`confirm`, `pick`, …). Routing logic:

- **Rule matched or top candidate exists** → Screen 1 (`confirm`) with
  the appropriate kind. Enter confirms; edits loop in-place.
- **No rule, no candidate** → Screen 2 (`pick`) → Screen 1 (`fresh_pick`).
- **Skip / quit** at any screen → return a proposal carrying that action.

Screen 4 (ambiguous match) and Screen 3 (collision) are not routed here
yet — they need additional pipeline plumbing (an "ambiguity threshold"
read of candidate scores, and a hook between `_build_result` and
`_persist_results`). The host stays narrow until those land so the
integration is provably no-regression against today's auto-pick-top
behaviour.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from rich.console import Console

from beancount_importer.categorizer.confirm import (
    ConfirmContext,
    ConfirmDecision,
    run as run_confirm,
)
from beancount_importer.categorizer.pick import (
    PickContext,
    PickDecision,
    run as run_pick,
)
from beancount_importer.matching.account_suggest import rank_accounts
from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import CategorizeContext


def make_screen_categorizer(
    console: Console,
) -> Callable[[CategorizeContext], CategoryProposal]:
    """Return a `CategorizeFn` that drives Screens 1 and 2.

    The closure has no per-session state — every call works from the
    `CategorizeContext` plus the global `Console`. That keeps the host
    cheap to construct and trivial to swap for a different impl in tests.
    """

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        # Path A: a rule matched OR a top candidate exists. Build a
        # provisional proposal from the strongest signal and let the
        # user confirm/edit in Screen 1.
        if ctx.matched_rule is not None or ctx.candidates:
            proposal, kind, matched_entry = _initial_proposal(ctx)
            return _run_confirm(console, ctx, proposal, kind, matched_entry)

        # Path B: nothing to suggest. Screen 2 picks an account, then
        # Screen 1 confirms. Skip/quit at either step short-circuits.
        return _run_pick_then_confirm(console, ctx)

    return _fn


# ── Path A: rule / top-candidate → Screen 1 ───────────────────────────────────


def _initial_proposal(
    ctx: CategorizeContext,
):
    """Build the seed proposal + Screen-1 kind when a rule or candidate hit.

    Returns `(proposal, kind, matched_entry_or_None)`. Rule wins over
    candidate — the user's authored override is more authoritative than
    a fuzzy match's target reuse.
    """
    if ctx.matched_rule is not None:
        rule = ctx.matched_rule
        return (
            CategoryProposal(
                action="categorize",
                postings=(Posting(account=rule.target_account),),
                payee=rule.override_payee,
                narration=rule.override_narration,
                tag=rule.tag,
                rule_used=rule,
            ),
            "auto_matched",
            None,
        )
    # ctx.candidates is non-empty (Path A's other branch).
    top_entry, _score = ctx.candidates[0]
    return (
        CategoryProposal(
            action="categorize",
            postings=(Posting(account=top_entry.target_account),),
        ),
        "top_candidate",
        top_entry,
    )


def _run_confirm(
    console: Console,
    ctx: CategorizeContext,
    proposal: CategoryProposal,
    kind: str,
    matched_entry,
) -> CategoryProposal:
    """Render Screen 1 and translate its decision into a `CategoryProposal`."""
    confirm_ctx = ConfirmContext(
        txn=ctx.txn,
        proposal=proposal,
        bank_account=ctx.source_account,
        kind=kind,  # type: ignore[arg-type]
        matched_rule=ctx.matched_rule,
        matched_entry=matched_entry,
        progress=ctx.progress,
        bank_key=ctx.txn.bank_key,
        year=ctx.txn.booking_date.year,
        active_tag=ctx.active_tag.tag if ctx.active_tag else None,
        tag_remaining=_tag_remaining(ctx),
    )
    decision = run_confirm(console, confirm_ctx)
    return _confirm_to_proposal(decision)


def _confirm_to_proposal(decision: ConfirmDecision) -> CategoryProposal:
    """Map a Screen-1 outcome to a `CategoryProposal` the pipeline understands."""
    if decision.action == "skip":
        return CategoryProposal(action="skip")
    if decision.action == "quit":
        return CategoryProposal(action="quit")
    # `confirm` always carries a proposal; the dataclass guarantees it.
    assert decision.proposal is not None
    return decision.proposal


# ── Path B: pick → confirm ────────────────────────────────────────────────────


def _run_pick_then_confirm(
    console: Console,
    ctx: CategorizeContext,
) -> CategoryProposal:
    """Screen 2 picks an account; Screen 1 confirms the resulting proposal.

    A `skip`/`quit` on Screen 2 returns immediately — no Screen 1
    prompt, since there's no proposal to confirm.
    """
    pick_decision = _ask_pick(console, ctx)
    if pick_decision.action == "skip":
        return CategoryProposal(action="skip")
    if pick_decision.action == "quit":
        return CategoryProposal(action="quit")

    assert pick_decision.account is not None  # `pick` always carries one
    seed = CategoryProposal(
        action="categorize",
        postings=(Posting(account=pick_decision.account),),
    )
    return _run_confirm(console, ctx, seed, kind="fresh_pick", matched_entry=None)


def _ask_pick(console: Console, ctx: CategorizeContext) -> PickDecision:
    """Build Screen 2's context and run it.

    Suggestion counts come from the same pool used to rank: every
    occurrence of an account on either side of an existing entry counts.
    """
    counts: Counter[str] = Counter()
    for entry in ctx.existing_entries:
        if entry.source_account:
            counts[entry.source_account] += 1
        if entry.target_account:
            counts[entry.target_account] += 1
    suggestions, all_accounts = rank_accounts(
        ctx.txn, ctx.candidates, ctx.existing_entries
    )
    pick_ctx = PickContext(
        txn=ctx.txn,
        bank_account=ctx.source_account,
        suggestions=tuple(suggestions),
        suggestion_counts=dict(counts),
        all_accounts=tuple(all_accounts),
        existing_entries=ctx.existing_entries,
        progress=ctx.progress,
        bank_key=ctx.txn.bank_key,
        year=ctx.txn.booking_date.year,
        active_tag=ctx.active_tag.tag if ctx.active_tag else None,
        tag_remaining=_tag_remaining(ctx),
    )
    return run_pick(console, pick_ctx)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _tag_remaining(ctx: CategorizeContext) -> int | None:
    """Days remaining for a `duration`-mode active tag; None otherwise.

    Computed inline rather than as a method on `ActiveTag` because the
    tag model is shared across persistence/serialisation paths and
    "days until expiry" is a UI-layer concern. The screens render the
    `(N left)` suffix iff this returns non-None.
    """
    tag = ctx.active_tag
    if tag is None or tag.mode != "duration" or tag.until_date is None:
        return None
    delta = (tag.until_date - ctx.txn.booking_date).days
    return max(0, delta)

"""Screen-driven `CategorizeFn` and `MergeFn` host.

Bridges the pipeline's user-facing slots (`CategorizeContext →
CategoryProposal`, `MergeContext → MergeDecision`) to the screen-based
prompt layer (`confirm`, `pick`, `ambiguous`, `collision`).

Routing — see `docs/screen-graph.md` for the full edge map:

- **Ambiguous candidates** (`ctx.is_ambiguous`) → Screen 4 → Screen 1 or
  Screen 2 depending on user choice.
- **Seed proposal exists** (rule matched or top candidate) → Screen 1
  with `kind=auto_matched` (rule) or `top_candidate`. Enter confirms;
  `[c]` round-trips to Screen 2 and back.
- **No seed** → Screen 2 (pick) → Screen 1 (`fresh_pick`).
- **Merge prompt** (pipeline detects an `update` would change fields)
  → Screen 3 (collision), six outcomes 1:1 with `MergeDecision.action`.

The pipeline pre-computes silent-skip and ambiguity detection (see
`CategorizeContext.seed_proposal` / `is_ambiguous`); by the time `_fn`
runs, there's a real choice to make.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from rich.console import Console

from beancount_importer.categorizer.ambiguous import (
    AmbiguousContext,
    run as run_ambiguous,
)
from beancount_importer.categorizer.collision import (
    CollisionContext,
    run as run_collision,
)
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
from beancount_importer.categorizer.screen import tag_remaining_days
from beancount_importer.matching.account_suggest import rank_accounts
from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import (
    CategorizeContext,
    MergeContext,
    MergeDecision,
)


def make_screen_categorizer(
    console: Console,
) -> Callable[[CategorizeContext], CategoryProposal]:
    """Return a `CategorizeFn` that drives Screens 1, 2, and 4.

    The pipeline pre-computes silent-skip and ambiguity detection: by the
    time `_fn` runs, we know there's a real choice to make. `ctx.is_ambiguous`
    routes to Screen 4; `ctx.seed_proposal` (set when a rule matched or a
    top candidate exists) seeds Screen 1; otherwise Path B (Screen 2 →
    Screen 1) picks from scratch.
    """

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        if ctx.is_ambiguous:
            return _run_ambiguous(console, ctx)
        if ctx.seed_proposal is not None:
            kind = "auto_matched" if ctx.matched_rule is not None else "top_candidate"
            matched_entry = ctx.candidates[0][0] if ctx.candidates else None
            return _run_confirm(console, ctx, ctx.seed_proposal, kind, matched_entry)
        return _run_pick_then_confirm(console, ctx)

    return _fn


def make_screen_merge_fn(
    console: Console,
) -> Callable[[MergeContext], MergeDecision]:
    """Return a `MergeFn` that drives Screen 3 (collision).

    Fires when the pipeline detects an `update` would change at least
    one field on the matched entry. The screen's six outcomes map 1:1
    to `MergeDecision.action` values; `_apply_merge_decision` in the
    pipeline does the result rewrite.
    """

    def _fn(ctx: MergeContext) -> MergeDecision:
        coll_ctx = CollisionContext(
            txn=ctx.txn,
            existing=ctx.matched_entry,
            proposed_changes=list(ctx.proposed_changes),
            proposal=ctx.proposal,
            progress=ctx.progress,
            bank_key=ctx.txn.bank_key,
            year=ctx.txn.booking_date.year,
            active_tag=ctx.active_tag.tag if ctx.active_tag else None,
            tag_remaining=_merge_tag_remaining(ctx),
        )
        decision = run_collision(console, coll_ctx)
        # `import_new` doesn't re-route through Screens 1/2 — by the
        # time we reach the merge prompt, Screen 1 has already produced
        # a proposal. The pipeline emits that proposal as a fresh entry
        # alongside the matched one (see `_apply_merge_decision`).
        return MergeDecision(action=decision.action)

    return _fn


def _merge_tag_remaining(ctx: MergeContext) -> int | None:
    return tag_remaining_days(ctx.active_tag, ctx.txn.booking_date)


# ── Path A0: ambiguous → Screen 4 → Screen 1 / Screen 2 ──────────────────────


def _run_ambiguous(
    console: Console,
    ctx: CategorizeContext,
) -> CategoryProposal:
    """Render Screen 4, then route by the user's decision.

    `pick`        → land on Screen 1 in `top_candidate` mode using the
                    selected entry's target as the seed proposal
    `import_new`  → fall through to Screen 2 (no rule, fresh account
                    pick), then Screen 1 in `fresh_pick` mode
    `skip` / `quit` → return immediately as a `skip`/`quit` proposal
    """
    amb_ctx = AmbiguousContext(
        txn=ctx.txn,
        candidates=ctx.candidates,
        progress=ctx.progress,
        bank_key=ctx.txn.bank_key,
        year=ctx.txn.booking_date.year,
        active_tag=ctx.active_tag.tag if ctx.active_tag else None,
        tag_remaining=_tag_remaining(ctx),
    )
    decision = run_ambiguous(console, amb_ctx)
    if (short := _short_circuit_proposal(decision.action)) is not None:
        return short
    if decision.action == "import_new":
        return _run_pick_then_confirm(console, ctx)
    # action == "pick" — Screen 4 guarantees an entry on this branch.
    assert decision.entry is not None
    seed = CategoryProposal(
        action="categorize",
        postings=(Posting(account=decision.entry.target_account),),
    )
    return _run_confirm(
        console, ctx, seed, kind="top_candidate", matched_entry=decision.entry
    )


# ── Path A: rule / top-candidate → Screen 1 ───────────────────────────────────


def _run_confirm(
    console: Console,
    ctx: CategorizeContext,
    proposal: CategoryProposal,
    kind: str,
    matched_entry,
) -> CategoryProposal:
    """Render Screen 1 and translate its decision into a `CategoryProposal`.

    Loops on `change_account`: Screen 1 → Screen 2 → Screen 1 (with
    `kind="fresh_pick"` and `matched_entry=None` since the user
    deliberately overrode whatever produced the seed). The user's
    in-flight edits (narration, payee, tag) carry across via the
    proposal returned from Screen 1.
    """
    while True:
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
            current_active_tag=ctx.active_tag,
            near_misses=ctx.near_misses if matched_entry is None else (),
        )
        decision = run_confirm(console, confirm_ctx)
        if decision.action != "change_account":
            return _confirm_to_proposal(decision)
        # `[c]` round-trip: pick a new account, then re-render Screen 1.
        # Skip / quit on Screen 2 short-circuits the whole categorize call.
        pick = _ask_pick(console, ctx)
        if (short := _short_circuit_proposal(pick.action)) is not None:
            return short
        assert pick.account is not None
        assert decision.proposal is not None
        proposal = decision.proposal.model_copy(
            update={"postings": (Posting(account=pick.account),)}
        )
        kind = "fresh_pick"
        matched_entry = None


def _confirm_to_proposal(decision: ConfirmDecision) -> CategoryProposal:
    """Map a Screen-1 outcome to a `CategoryProposal` the pipeline understands."""
    if (short := _short_circuit_proposal(decision.action)) is not None:
        return short
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
    if (short := _short_circuit_proposal(pick_decision.action)) is not None:
        return short

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
        near_misses=ctx.near_misses,
    )
    return run_pick(console, pick_ctx)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _tag_remaining(ctx: CategorizeContext) -> int | None:
    return tag_remaining_days(ctx.active_tag, ctx.txn.booking_date)


def _short_circuit_proposal(action: str) -> CategoryProposal | None:
    """Translate a screen's `skip` / `quit` action into a bubble-up proposal.

    Returns None for any other action so the caller falls through to
    its own routing. Centralises the otherwise-duplicated two-line
    `if action == "skip" / "quit": return ...` boilerplate that every
    screen-runner needs.
    """
    if action == "skip":
        return CategoryProposal(action="skip")
    if action == "quit":
        return CategoryProposal(action="quit")
    return None

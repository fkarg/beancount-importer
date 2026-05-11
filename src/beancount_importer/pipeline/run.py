"""Pipeline: turn source transactions into ImportResults.

The pipeline is deterministic given inputs. It does no I/O beyond reading CSV
sources, reading existing ledger files, and (via DecisionLog) appending one
JSONL line per non-trivial decision. It does NOT write the ledger — the CLI
collects ImportResults and applies splices/appends after the run.

Iteration-local mutable state (working rules and active tag) lives only for
one `run()` call. The session itself is frozen.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from beancount_importer.beancount_io.writer import format_transaction
from beancount_importer.config import BankConfig
from beancount_importer.matching.dedup import find_definitive_duplicate
from beancount_importer.matching.registry import (
    MatcherHook,
    MatchOutcome,
    first_outcome,
    load_matchers,
)
from beancount_importer.matching.scorer import find_candidates
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.pipeline._clean import clean_paypal_noise
from beancount_importer.pipeline._shared import (
    _load_all_outputs,
    _parse_all_inputs,
    _select_banks,
)
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.engine import find_matching_rule
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag, TagStateDelta
from beancount_importer.session import ImportSession
from beancount_importer.transforms import apply_transforms, load_transforms


# ── Public types ──────────────────────────────────────────────────────────────


class NearMiss(BaseModel):
    """A close-but-not-quite match surfaced for diagnostic display.

    Computed by the pipeline only when no real candidates land — exists
    purely to give the user a readable answer to "why is this row being
    prompted instead of dedup-skipped?".

    Two reasons:
    - `below_threshold`: same source-account bucket, scored under `min_score`.
      Usually means rule-cleaned narration drifted under the cutoff.
    - `different_bucket`: same currency + |amount| within date tolerance, but
      the entry's `source_account` doesn't match the txn's bank. Catches the
      sub-account case (entry on `Assets:B:SPK:Checking` while txn buckets
      to `Assets:B:SPK`) and other misfiled placements.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    entry: LedgerEntry
    score: float
    reason: Literal["below_threshold", "different_bucket"]


class CategorizeContext(BaseModel):
    """Inputs supplied to a CategorizeFn for one transaction.

    `existing_entries` is the full ledger universe across all banks; the
    categorizer can derive ranked account suggestions from it via
    `matching.account_suggest.rank_accounts`. `account_hints` is a
    pre-computed shortcut populated by the pipeline.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    txn: SourceTransaction
    rules: tuple[CategorizationRule, ...]
    candidates: tuple[tuple[LedgerEntry, float], ...] = ()
    matched_rule: CategorizationRule | None = None
    account_hints: tuple[str, ...] = ()
    active_tag: ActiveTag | None = None
    existing_entries: tuple[LedgerEntry, ...] = ()
    # Source-side account (e.g. `Assets:B:SPK`) and run progress; needed by
    # the screen-driven categorizer to render the headline + state header.
    source_account: str = ""
    progress: tuple[int, int] = (0, 0)
    # Diagnostic-only: populated only when `candidates` is empty, surfaces
    # why the user is being prompted instead of seeing a silent skip.
    near_misses: tuple[NearMiss, ...] = ()
    # Pre-computed routing hints. The pipeline owns silent-skip detection
    # (zero-diff updates never reach categorize_fn) and ambiguity detection
    # (top two candidates within `min_delta`). The host uses these to pick
    # which screen to render without redoing the work.
    seed_proposal: CategoryProposal | None = None
    is_ambiguous: bool = False


CategorizeFn = Callable[[CategorizeContext], CategoryProposal]


class MergeContext(BaseModel):
    """Inputs supplied to a `MergeFn` when an `update` would change fields.

    Fires after `_build_result` decides this txn matches an existing entry
    AND the resulting `proposed_changes` is non-empty. Lets the host
    (cli.py) prompt the user via Screen 3 before the splice happens.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    txn: SourceTransaction
    proposal: CategoryProposal
    matched_entry: LedgerEntry
    proposed_changes: tuple[ProposedChange, ...]
    progress: tuple[int, int] = (0, 0)
    active_tag: ActiveTag | None = None


class MergeDecision(BaseModel):
    """Returned from a `MergeFn`. The pipeline routes on `action`:

    - `update`     → keep the auto-generated update result as-is
    - `keep`       → silent-match (no splice; replay reproduces silently)
    - `import_new` → create a fresh entry instead of updating the matched one
    - `block`      → install a `suppress_updates` rule and skip this row
    - `skip`       → no-op for this run; row reappears next run
    - `quit`       → tear down the run
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["update", "keep", "import_new", "block", "skip", "quit"]


MergeFn = Callable[[MergeContext], MergeDecision]


@runtime_checkable
class Reporter(Protocol):
    """Receives all user-visible output from the pipeline."""

    def on_result(self, result: ImportResult) -> None: ...
    def on_progress(self, current: int, total: int, bank: str) -> None: ...
    def on_error(self, message: str) -> None: ...


class NoopReporter:
    """Discard all events; useful in tests."""

    def on_result(self, result: ImportResult) -> None:
        del result

    def on_progress(self, current: int, total: int, bank: str) -> None:
        del current, total, bank

    def on_error(self, message: str) -> None:
        del message


# ── Pipeline ──────────────────────────────────────────────────────────────────


def run(
    session: ImportSession,
    base_dir: Path,
    categorize_fn: CategorizeFn,
    reporter: Reporter,
    decisions: DecisionLog | None = None,
    merge_fn: MergeFn | None = None,
    results_accumulator: list[ImportResult] | None = None,
) -> list[ImportResult]:
    """Execute the import pipeline.

    `base_dir` is the directory used to resolve all relative paths in the config
    (typically the directory containing `import_config.toml`). The session's
    `options.year_filter` (if any) scopes which transactions are processed;
    output paths are resolved against each transaction's own booking year by
    the CLI after the pipeline returns.

    `results_accumulator`, if provided, is the same list returned on
    success — it lets the caller observe the partial state on
    `KeyboardInterrupt` (which the pipeline doesn't catch). The CLI uses
    this to persist rules created mid-run even when the user rage-quits.
    """
    decisions = decisions if decisions is not None else DecisionLog(None)
    config = session.config
    year_filter = session.options.year_filter

    banks = _select_banks(session)
    inputs = _parse_all_inputs(banks, base_dir, year_filter, reporter)

    # Single global sweep of every .bean under transactions_dir. The result is
    # one entry per bank-shaped posting; matching looks up the per-account
    # bucket below by `bank.account`. Cross-bank transit legs surface in their
    # own buckets without per-bank rglobs.
    bank_accounts = tuple({b.account for b in banks})
    existing = _load_all_outputs(
        base_dir,
        config.transactions_dir,
        account_prefixes=tuple(config.matching.internal_transfer_account_prefixes),
        extra_accounts=bank_accounts,
        metadata_date_keys=tuple(config.matching.metadata_date_keys),
        synthesize_from_metadata=dict(config.matching.synthesize_from_metadata),
    )
    existing_by_account: dict[str, list[LedgerEntry]] = {}
    for entry in existing:
        existing_by_account.setdefault(entry.source_account, []).append(entry)

    bank_account_by_key = {b.key: b.account for b in banks}
    bank_by_key = {b.key: b for b in banks}

    transforms = load_transforms(config.transforms.enabled)
    matchers = load_matchers(list(config.matching.enabled_matchers))
    working_rules: list[CategorizationRule] = list(session.rules)
    working_tag: ActiveTag | None = session.tag_state.active

    # Pre-bucket all CSV rows by bank key so cross-source matchers can scan
    # the PayPal CSV (or any other bank) without re-iterating `inputs` per call.
    csv_by_bank: dict[str, list[SourceTransaction]] = {}
    for t in inputs:
        csv_by_bank.setdefault(t.bank_key, []).append(t)

    total = len(inputs)
    results: list[ImportResult] = (
        results_accumulator if results_accumulator is not None else []
    )

    for progress, txn in enumerate(inputs, start=1):
        reporter.on_progress(progress, total, txn.bank_key)
        # Pre-match cleaner: tidies SPK-style PayPal rows so the
        # merge-prompt display and text-similarity scoring see a
        # clean payee + description instead of bank transport noise.
        # Other rows pass through unchanged.
        txn = clean_paypal_noise(txn)

        bank_cfg = bank_by_key[txn.bank_key]
        bucket = existing_by_account.get(bank_account_by_key[txn.bank_key], [])
        result, working_tag, working_rules = _process_transaction(
            txn=txn,
            bank=bank_cfg,
            existing=bucket,
            existing_all=existing,
            csv_by_bank=csv_by_bank,
            matchers=matchers,
            config=config,
            working_rules=working_rules,
            working_tag=working_tag,
            transforms_hooks=transforms,
            categorize_fn=categorize_fn,
            merge_fn=merge_fn,
            decisions=decisions,
            auto_threshold=session.options.auto_threshold,
            progress=(progress, total),
        )

        counter = _synthesize_counter_leg(
            result,
            txn,
            internal_prefixes=tuple(config.matching.internal_transfer_account_prefixes),
            bank_accounts=set(bank_accounts),
        )
        if counter is not None:
            existing_by_account.setdefault(counter.source_account, []).append(counter)
            existing.append(counter)

        decisions.record(txn, result)
        reporter.on_result(result)
        results.append(result)

        if result.action == "quit":
            break

    return results


# ── Per-transaction processing ────────────────────────────────────────────────


@dataclass(frozen=True)
class _TxnState:
    """Per-transaction state threaded through the processing phases.

    Inputs (`txn`, `bank`, `progress`) and the carried-forward session
    state (`working_rules`, `working_tag`) flow in via the constructor.
    Each phase that produces a value (`rule`, `proposal`, …) returns an
    `evolve`d copy with the new field populated. Phases never mutate.

    Buckets (`existing`, `existing_all`) and other cross-cutting params
    stay as explicit function arguments rather than fields here — they
    live across many transactions and threading lists through frozen
    state would just be ceremony.
    """

    txn: SourceTransaction
    bank: BankConfig
    progress: tuple[int, int]
    working_rules: tuple[CategorizationRule, ...]
    working_tag: ActiveTag | None

    rule: CategorizationRule | None = None
    match_txn: SourceTransaction | None = None
    candidates: tuple[tuple[LedgerEntry, float], ...] = ()
    matcher_proposal: CategoryProposal | None = None
    proposal: CategoryProposal | None = None
    new_rule: CategorizationRule | None = None
    user_tag_delta: TagStateDelta | None = None
    tag_delta: TagStateDelta | None = None
    next_tag: ActiveTag | None = None

    def evolve(self, **changes) -> _TxnState:
        return replace(self, **changes)


def _process_transaction(
    *,
    txn: SourceTransaction,
    bank: BankConfig,
    existing: list[LedgerEntry],
    existing_all: list[LedgerEntry],
    csv_by_bank: dict[str, list[SourceTransaction]],
    matchers: list[MatcherHook],
    config,  # Config; type omitted to avoid a circular import at type level
    working_rules: list[CategorizationRule],
    working_tag: ActiveTag | None,
    transforms_hooks,
    categorize_fn: CategorizeFn,
    merge_fn: MergeFn | None,
    decisions: DecisionLog,
    auto_threshold: float | None,
    progress: tuple[int, int] = (0, 0),
) -> tuple[ImportResult, ActiveTag | None, list[CategorizationRule]]:
    """Process one txn, returning the result + updated tag + updated rules list.

    Composed from named phase helpers operating on a frozen `_TxnState`. The
    short-circuit phase tries replay → dedup → skip-pattern → matcher in
    order; any hit emits a terminal result. The proposal phase scores
    candidates and selects between matcher rewrite, auto-threshold rule,
    silent-skip seed, or `categorize_fn`. The post-process phase folds in
    transforms, tag state, and the save-as-rule synthesis. The finalise
    phase builds the result, runs the merge prompt, and claims the matched
    entry.
    """
    state = _TxnState(
        txn=txn,
        bank=bank,
        progress=progress,
        working_rules=tuple(working_rules),
        working_tag=working_tag,
    )
    state = _resolve_rule(state)
    advanced_tag = _advance_tag(state.working_tag, txn.booking_date)

    # Short-circuit phase: each helper returns an `ImportResult` for its
    # own outcome, or `None` to fall through. Helpers that consume an
    # entry mutate `existing` / `existing_all` in place — pragmatic given
    # the buckets are shared across the whole run.
    short_circuit = _short_circuit(
        state,
        decisions=decisions,
        existing=existing,
        existing_all=existing_all,
        csv_by_bank=csv_by_bank,
        matchers=matchers,
        config=config,
    )
    if isinstance(short_circuit, ImportResult):
        return short_circuit, advanced_tag, list(state.working_rules)
    state = short_circuit  # may carry a matcher_proposal forward

    # Proposal phase.
    state = _score_candidates(
        state,
        existing=existing,
        min_score=config.matching.min_score,
        max_date_days=config.matching.max_date_days,
    )
    state = _resolve_proposal(
        state,
        config=config,
        existing=existing,
        existing_all=existing_all,
        bank=bank,
        auto_threshold=auto_threshold,
        categorize_fn=categorize_fn,
    )

    # Post-process the proposal: transforms, user tag delta, active tag
    # auto-stamp, save-as-rule, tag delta for persistence.
    state = _apply_transforms(state, transforms_hooks)
    state = _apply_user_tag_delta(state)
    state = _stamp_active_tag(state)
    state = _maybe_save_as_rule(state)
    state = _compute_tag_delta(state)

    # Finalise: build result, optional merge prompt, claim entry.
    # `_resolve_proposal` always sets `state.proposal` on this path —
    # the assert is a contract pin for type-checkers.
    assert state.proposal is not None
    result = _build_result(
        txn=txn,
        bank=bank,
        proposal=state.proposal,
        existing=existing,
        matched_rule=state.rule,
        is_replay=False,
        new_rule=state.new_rule,
        tag_state_delta=state.tag_delta,
        min_score=config.matching.min_score,
        max_date_days=config.matching.max_date_days,
        match_txn=state.match_txn,
        narration_max_length=config.narration_max_length,
        paypal_account=config.paypal_account,
    )

    new_rules_list = list(state.working_rules)
    if (
        merge_fn is not None
        and result.action == "update"
        and result.proposed_changes
        and result.matched_entry is not None
        and result.proposal is not None
    ):
        merge_decision = merge_fn(
            MergeContext(
                txn=txn,
                proposal=result.proposal,
                matched_entry=result.matched_entry,
                proposed_changes=tuple(result.proposed_changes),
                progress=progress,
                active_tag=state.working_tag,
            )
        )
        result, new_rules_list = _apply_merge_decision(
            result,
            merge_decision,
            txn,
            bank,
            list(state.working_rules),
            narration_max_length=config.narration_max_length,
        )

    # Claim the matched entry only on outcomes that actually consume it
    # (see `_claim_matched_entry`).
    _claim_matched_entry(result, existing)
    return result, state.next_tag, new_rules_list


# ── Phase helpers (state in, state or terminal out) ──────────────────────────


def _resolve_rule(state: _TxnState) -> _TxnState:
    """Look up the matching rule and pre-rewrite the txn for matching.

    Dedup and the scorer compare against existing entries that were
    written with rule overrides applied, so the txn side has to mirror
    those overrides for the texts to line up. Without this pre-rewrite,
    the second import of a rule-affected row content-hash-mismatches its
    own previously-written entry and gets re-prompted. Replay still wins
    over rules — it's a stronger signal — but it short-circuits before
    we get here.
    """
    rule = find_matching_rule(state.txn, list(state.working_rules))
    return state.evolve(rule=rule, match_txn=_apply_rule_overrides(state.txn, rule))


def _short_circuit(
    state: _TxnState,
    *,
    decisions: DecisionLog,
    existing: list[LedgerEntry],
    existing_all: list[LedgerEntry],
    csv_by_bank: dict[str, list[SourceTransaction]],
    matchers: list[MatcherHook],
    config,
) -> ImportResult | _TxnState:
    """Try the four short-circuit paths in order, returning the first hit.

    Returns an `ImportResult` if a path fires (the caller emits it as the
    txn's terminal outcome), or the (possibly evolved) state if every
    path falls through. The matcher path may evolve state with a
    `matcher_proposal` even when it doesn't short-circuit.
    """
    replay = _try_replay(state, decisions=decisions, existing=existing, config=config)
    if replay is not None:
        return replay

    dedup = _try_dedup(
        state,
        existing=existing,
        max_date_days=config.matching.dedup_max_date_days,
    )
    if dedup is not None:
        return dedup

    skip = _try_skip_pattern(state, config=config)
    if skip is not None:
        return skip

    return _try_matcher(
        state,
        matchers=matchers,
        csv_by_bank=csv_by_bank,
        existing=existing,
        existing_all=existing_all,
    )


def _try_replay(
    state: _TxnState,
    *,
    decisions: DecisionLog,
    existing: list[LedgerEntry],
    config,
) -> ImportResult | None:
    """If a past decision matches this txn, reproduce its result."""
    replayed = decisions.lookup(state.txn)
    if replayed is None:
        return None
    result = _build_result(
        txn=state.txn,
        bank=state.bank,
        proposal=replayed,
        existing=existing,
        matched_rule=None,
        is_replay=True,
        new_rule=None,
        tag_state_delta=None,
        min_score=config.matching.min_score,
        max_date_days=config.matching.max_date_days,
        match_txn=state.match_txn,
        narration_max_length=config.narration_max_length,
        paypal_account=config.paypal_account,
    )
    _claim_matched_entry(result, existing)
    return result


def _try_dedup(
    state: _TxnState,
    *,
    existing: list[LedgerEntry],
    max_date_days: int,
) -> ImportResult | None:
    """Drop confirmed duplicates and claim the matched entry.

    Without claiming, two identical CSVs would both point at entry A
    and leave entry B looking like a CSV-orphan in the preview report.
    `find_definitive_duplicate` deliberately returns None when 2+
    candidates fit; those fall through to the scorer + merge prompt
    so the user disambiguates instead of dedup picking blindly.
    """
    assert state.match_txn is not None  # set by _resolve_rule
    duplicate = find_definitive_duplicate(
        state.match_txn, existing, max_date_days=max_date_days
    )
    if duplicate is None:
        return None
    existing.remove(duplicate)
    return ImportResult(
        source_txn=state.txn,
        action="skip",
        proposal=None,
        skip_reason="duplicate",
        matched_entry=duplicate,
    )


def _try_skip_pattern(state: _TxnState, *, config) -> ImportResult | None:
    """Hard suppression — `skip_update_patterns` drops the proposal entirely."""
    if not _matches_skip_pattern(config.skip_update_patterns, state.txn):
        return None
    return ImportResult(
        source_txn=state.txn,
        action="skip",
        proposal=None,
        skip_reason="skip_rule",
    )


def _try_matcher(
    state: _TxnState,
    *,
    matchers: list[MatcherHook],
    csv_by_bank: dict[str, list[SourceTransaction]],
    existing: list[LedgerEntry],
    existing_all: list[LedgerEntry],
) -> ImportResult | _TxnState:
    """Cross-source matchers: skip-when-already-booked or rewrite_target.

    On `skip` outcomes, claim the matched entry across both bucket views
    so a parallel CSV row doesn't re-attribute to it. On `rewrite_target`,
    stash the synthesized proposal on state and let the proposal phase
    pick it up; the categorizer is bypassed for this txn.
    """
    outcome = first_outcome(matchers, state.txn, csv_by_bank, existing_all)
    if outcome is None:
        return state
    if outcome.kind == "skip":
        # Shipped matchers always set `matched_entry` on a skip outcome
        # and pull it from `existing_all`; the protocol allows None for
        # forward-compat with heuristic matchers that skip without
        # naming an entry. The bank-scoped bucket may or may not contain
        # it (cross-source matchers usually return an entry on a
        # different bank).
        matched = outcome.matched_entry
        if matched is None:  # pragma: no cover - defensive
            pass
        else:
            existing_all.remove(matched)
            if matched in existing:
                existing.remove(matched)
        return ImportResult(
            source_txn=state.txn,
            action="skip",
            proposal=None,
            skip_reason="cross_source_match",
            matched_entry=matched,
        )
    return state.evolve(matcher_proposal=_proposal_from_outcome(outcome))


def _score_candidates(
    state: _TxnState,
    *,
    existing: list[LedgerEntry],
    min_score: float,
    max_date_days: int,
) -> _TxnState:
    """Score the rule-rewritten txn against `existing` and stash candidates."""
    assert state.match_txn is not None
    candidates = find_candidates(
        state.match_txn,
        existing,
        min_score=min_score,
        max_date_days=max_date_days,
    )
    return state.evolve(candidates=tuple(candidates))


def _resolve_proposal(
    state: _TxnState,
    *,
    config,
    existing: list[LedgerEntry],
    existing_all: list[LedgerEntry],
    bank: BankConfig,
    auto_threshold: float | None,
    categorize_fn: CategorizeFn,
) -> _TxnState:
    """Choose the proposal source.

    Order of precedence (each falls through if it doesn't apply):
    1. Matcher rewrite (`rewrite_target` outcome) — ground truth from
       cross-source data, preempts both auto-threshold and the prompt.
    2. Auto-threshold + rule — high-confidence candidate match with a
       rule attached, applied without a prompt.
    3. Pipeline silent-skip — seed proposal would produce no diff
       against the matched entry; the user has nothing to consent to.
    4. `categorize_fn` — the user (or a stub) chooses.
    """
    if state.matcher_proposal is not None:
        return state.evolve(proposal=state.matcher_proposal)

    if (
        auto_threshold is not None
        and state.candidates
        and state.candidates[0][1] >= auto_threshold
        and state.rule is not None
    ):
        return state.evolve(proposal=_proposal_from_rule(state.rule))

    silent = _silent_skip_proposal(
        state.rule, state.candidates, config.matching.min_delta
    )
    if silent is not None:
        return state.evolve(proposal=silent)

    from beancount_importer.matching.account_suggest import rank_accounts

    suggested = state.rule.target_account if state.rule is not None else None
    hints, _ = rank_accounts(
        state.txn, list(state.candidates), existing_all, suggested_target=suggested
    )
    # Diagnostic near-misses: only computed when nothing crossed the
    # threshold, so the screens can render an explanatory line above the
    # hotkeys instead of leaving the user guessing why dedup didn't
    # silent-skip the row.
    assert state.match_txn is not None
    near_misses = (
        _compute_near_misses(
            state.match_txn,
            in_bucket=existing,
            cross_bucket=existing_all,
            bank_account=bank.account,
            min_score=config.matching.min_score,
            max_date_days=config.matching.max_date_days,
        )
        if not state.candidates
        else ()
    )
    context = CategorizeContext(
        txn=state.txn,
        rules=state.working_rules,
        candidates=state.candidates,
        matched_rule=state.rule,
        account_hints=tuple(hints),
        active_tag=state.working_tag,
        existing_entries=tuple(existing_all),
        source_account=bank.account,
        progress=state.progress,
        near_misses=near_misses,
        seed_proposal=_seed_proposal(state.rule, state.candidates),
        is_ambiguous=(
            state.rule is None
            and _is_ambiguous_match(state.candidates, config.matching.min_delta)
        ),
    )
    return state.evolve(proposal=categorize_fn(context))


def _apply_transforms(state: _TxnState, transforms_hooks) -> _TxnState:
    """Run rule-driven transform hooks against the proposal."""
    if state.rule is None or state.proposal is None:
        return state
    return state.evolve(
        proposal=apply_transforms(transforms_hooks, state.proposal, state.txn, state.rule)
    )


def _apply_user_tag_delta(state: _TxnState) -> _TxnState:
    """Fold a Screen-1 `[t]` outcome into the working tag.

    The proposal arrives with `tag_state_delta` set; we stash the delta
    and update `working_tag` BEFORE the auto-stamp step so this txn
    picks up the new tag. The delta also surfaces on the result later
    (via `_compute_tag_delta`) for persistence.
    """
    assert state.proposal is not None
    delta = state.proposal.tag_state_delta
    if delta is None:
        return state.evolve(user_tag_delta=None)
    if delta.op == "set":
        return state.evolve(user_tag_delta=delta, working_tag=delta.new_state)
    # delta.op == "clear"
    return state.evolve(user_tag_delta=delta, working_tag=None)


def _stamp_active_tag(state: _TxnState) -> _TxnState:
    """Stamp the active tag onto the proposal if applicable."""
    assert state.proposal is not None
    tag = state.working_tag
    if tag is None or state.proposal.tag is not None:
        return state
    if not tag.applies_to(state.txn.booking_date):
        return state
    return state.evolve(proposal=state.proposal.model_copy(update={"tag": tag.tag}))


def _maybe_save_as_rule(state: _TxnState) -> _TxnState:
    """Synthesize a rule from the proposal if the user asked to save it."""
    assert state.proposal is not None
    if not (state.proposal.save_as_rule and state.proposal.action == "categorize"):
        return state
    new_rule = _derive_rule(state.txn, state.proposal)
    if new_rule is None:
        return state
    return state.evolve(
        new_rule=new_rule,
        working_rules=(*state.working_rules, new_rule),
    )


def _compute_tag_delta(state: _TxnState) -> _TxnState:
    """Compute the persisted tag-state delta + the next-txn working tag.

    User intent dominates: a user `set` / `clear` is recorded verbatim.
    Otherwise we fall back to the auto-clear that fires when `once`
    mode expires or `duration` runs past `until_date`.
    """
    next_tag = _advance_tag(state.working_tag, state.txn.booking_date)
    if state.user_tag_delta is not None:
        return state.evolve(next_tag=next_tag, tag_delta=state.user_tag_delta)
    if next_tag != state.working_tag and state.working_tag is not None:
        return state.evolve(next_tag=next_tag, tag_delta=TagStateDelta(op="clear"))
    return state.evolve(next_tag=next_tag, tag_delta=None)


def _apply_merge_decision(
    result: ImportResult,
    decision: MergeDecision,
    txn: SourceTransaction,
    bank: BankConfig,
    working_rules: list[CategorizationRule],
    *,
    narration_max_length: int | None = None,
) -> tuple[ImportResult, list[CategorizationRule]]:
    """Translate a Screen-3 outcome into a finalised `ImportResult`.

    `working_rules` may grow when the user picks `block` (we install a
    `suppress_updates` rule so future runs auto-skip). Decisions that
    don't touch rules return `working_rules` unchanged.
    """
    assert result.matched_entry is not None  # gated by the caller
    assert result.proposal is not None
    entry = result.matched_entry

    if decision.action == "update":
        # Default: keep the auto-generated update result as-is.
        return result, working_rules

    if decision.action == "keep":
        # Silent match — record a proposal that mirrors the existing
        # entry so replay reproduces the same empty-diff outcome next
        # run, without re-prompting.
        mirror = _proposal_from_entry(entry)
        kept = result.model_copy(
            update={
                "action": "update",
                "proposed_changes": [],
                "proposal": mirror,
                "skip_reason": "user_kept",
            }
        )
        return kept, working_rules

    if decision.action == "skip":
        return (
            result.model_copy(
                update={
                    "action": "skip",
                    "matched_entry": None,
                    "proposed_changes": [],
                    "proposal": None,
                    "skip_reason": "user_skipped",
                }
            ),
            working_rules,
        )

    if decision.action == "quit":
        return (
            result.model_copy(
                update={
                    "action": "quit",
                    "matched_entry": None,
                    "proposed_changes": [],
                }
            ),
            working_rules,
        )

    if decision.action == "import_new":
        # Fresh entry instead of touching the matched one. The proposal
        # already came from the categorizer; we just reformat it as a
        # new-entry text and clear the matched-entry pointer.
        new_text = _format_new_entry(
            bank, txn, result.proposal, narration_max_length=narration_max_length
        )
        return (
            result.model_copy(
                update={
                    "action": "new",
                    "matched_entry": None,
                    "proposed_changes": [],
                    "new_entry_text": new_text,
                }
            ),
            working_rules,
        )

    # decision.action == "block" — install a skip-update rule for this
    # payee and skip the current row. Future runs match the rule and
    # produce a `skip_rule` result without ever reaching Screen 3.
    block_rule = _block_update_rule(txn, entry)
    return (
        result.model_copy(
            update={
                "action": "skip",
                "matched_entry": None,
                "proposed_changes": [],
                "proposal": None,
                "skip_reason": "user_blocked",
                "new_rule": block_rule,
            }
        ),
        [*working_rules, block_rule] if block_rule else working_rules,
    )


def _proposal_from_entry(entry: LedgerEntry) -> CategoryProposal:
    """Build a categorize proposal that exactly mirrors `entry`.

    Used for the Screen-3 `keep` branch: a proposal matching the existing
    entry produces an empty `_diff_changes` and replays as a silent
    skip on subsequent runs.
    """
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=entry.target_account),),
        payee=entry.payee,
        narration=entry.narration,
    )


def _block_update_rule(
    txn: SourceTransaction, entry: LedgerEntry
) -> CategorizationRule | None:
    """Synthesize a `suppress_updates=True` rule that matches `txn`.

    Prefers payee-based matching; falls back to description if payee is
    absent. Returns None when neither field is available — the caller
    treats this as "block didn't take" and downgrades to a plain skip.
    """
    if txn.payee:
        pattern = _escape_for_regex(txn.payee)
        return CategorizationRule(
            target_account=entry.target_account,
            payee_pattern=pattern,
            bank_key=txn.bank_key,
            suppress_updates=True,
        )
    if txn.description:
        return CategorizationRule(
            target_account=entry.target_account,
            description_pattern=_escape_for_regex(txn.description),
            bank_key=txn.bank_key,
            suppress_updates=True,
        )
    return None


def _build_result(
    *,
    txn: SourceTransaction,
    bank: BankConfig,
    proposal: CategoryProposal,
    existing: list[LedgerEntry],
    matched_rule: CategorizationRule | None,
    is_replay: bool,
    new_rule: CategorizationRule | None,
    tag_state_delta: TagStateDelta | None,
    min_score: float,
    max_date_days: int,
    match_txn: SourceTransaction | None = None,
    narration_max_length: int | None = None,
    paypal_account: str | None = None,
) -> ImportResult:
    """Assemble the per-row result.

    `match_txn` carries any rule-driven payee/narration overrides used during
    candidate scoring; if omitted (e.g. on the replay path that doesn't
    pre-rewrite), we fall back to the raw `txn`. The split lets the call site
    own the rule semantics without duplicating overrides logic here.
    """
    if proposal.action == "skip":
        return ImportResult(
            source_txn=txn,
            action="skip",
            proposal=proposal,
            rule_matched=matched_rule,
            is_replay=is_replay,
        )
    if proposal.action == "quit":
        return ImportResult(
            source_txn=txn,
            action="quit",
            proposal=proposal,
            rule_matched=matched_rule,
            is_replay=is_replay,
        )

    # categorize action: decide new vs. update by inspecting top candidate.
    score_txn = match_txn if match_txn is not None else txn
    candidates = find_candidates(
        score_txn, existing, min_score=min_score, max_date_days=max_date_days
    )
    best: LedgerEntry | None = candidates[0][0] if candidates else None
    action = "update" if best is not None else "new"

    proposed_changes: list[ProposedChange] = []
    new_entry_text = ""
    if best is not None:
        if best.amount_inferred:
            proposed_changes, proposal = _propose_date_metadata(
                txn, best, proposal, paypal_account=paypal_account
            )
        else:
            proposed_changes = _diff_changes(best, proposal, matched_rule)
    else:
        new_entry_text = _format_new_entry(
            bank, txn, proposal, narration_max_length=narration_max_length
        )

    return ImportResult(
        source_txn=txn,
        action=action,  # type: ignore[arg-type]
        matched_entry=best,
        proposed_changes=proposed_changes,
        new_entry_text=new_entry_text,
        proposal=proposal,
        rule_matched=matched_rule,
        is_replay=is_replay,
        new_rule=new_rule,
        tag_state_delta=tag_state_delta,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _claim_matched_entry(result: ImportResult, bucket: list[LedgerEntry]) -> None:
    """Remove the matched entry from `bucket` if `result` actually consumed it.

    Called once per txn after the merge prompt has resolved (or after the
    replay path emits its result). Only `update` outcomes consume an entry —
    `skip`/`new`/`quit` either declined to use the candidate or never had
    one. Without this gating, a user-driven `import_new` / `skip` / `block`
    decision would silently drop the matched entry from the in-session
    bucket, leaving subsequent identical CSV rows unable to find it.
    """
    if result.action != "update" or result.matched_entry is None:
        return
    bucket.remove(result.matched_entry)


def _apply_rule_overrides(
    txn: SourceTransaction, rule: CategorizationRule | None
) -> SourceTransaction:
    """Return a copy of `txn` with rule-driven payee/narration overrides folded in.

    Rules can set `override_payee` / `override_narration`; when they do, the
    importer writes those values into the ledger entry it creates. Dedup and
    candidate scoring need to compare like-with-like, so the txn's matching
    fields are pre-substituted to mirror what would land on disk. When the
    rule sets neither override (or no rule fires), the original txn is
    returned unchanged. Identity-preserving so caller code can compare via
    `is` for a quick "did anything change?" check.
    """
    if rule is None:
        return txn
    overrides: dict[str, str] = {}
    if rule.override_payee:
        overrides["payee"] = rule.override_payee
    if rule.override_narration:
        overrides["description"] = rule.override_narration
    return txn.model_copy(update=overrides) if overrides else txn


def _compute_near_misses(
    txn: SourceTransaction,
    *,
    in_bucket: list[LedgerEntry],
    cross_bucket: list[LedgerEntry],
    bank_account: str,
    min_score: float,
    max_date_days: int,
) -> tuple[NearMiss, ...]:
    """Surface the closest "almost-a-match" entry for diagnostic display.

    Two passes, each contributing at most one entry so the screens render a
    single explanatory line. The order — in-bucket first — reflects the more
    common cause of unexpected re-prompting (text drifted under `min_score`)
    and yields a more actionable hint than the cross-bucket case.

    The cross-bucket pass deliberately skips entries with `amount_inferred`
    (cross-bank transit legs handled by the scorer's reversed-sign path) so
    the diagnostic doesn't double up on a relationship the scorer already
    surfaces via candidates.
    """
    misses: list[NearMiss] = []

    # In-bucket: same source_account, scorer ran but everything fell below
    # min_score. Re-score with no floor and pick the best below the cutoff.
    below = find_candidates(
        txn, in_bucket, min_score=0.0, max_date_days=max_date_days
    )
    for entry, score in below:
        if score < min_score:
            misses.append(NearMiss(entry=entry, score=score, reason="below_threshold"))
            break

    # Cross-bucket: an entry with the same currency + |amount| within date
    # tolerance lives on a *different* source_account. Catches sub-account
    # placements (`Assets:B:SPK:Checking` when the txn buckets to
    # `Assets:B:SPK`) without claiming or otherwise touching the entry.
    target_amount = abs(txn.amount)
    closest: tuple[int, LedgerEntry] | None = None
    for entry in cross_bucket:
        if entry.source_account == bank_account:
            continue
        if entry.amount_inferred:
            continue
        if entry.currency != txn.currency:
            continue
        if abs(entry.amount) != target_amount:
            continue
        days = abs((entry.date - txn.booking_date).days)
        if days > max_date_days:
            continue
        if closest is None or days < closest[0]:
            closest = (days, entry)
    if closest is not None:
        # Synthesize a confidence figure analogous to the scorer's date
        # proximity term so the screen can render a single comparable score.
        score = 1.0 - (closest[0] / max_date_days)
        misses.append(NearMiss(entry=closest[1], score=score, reason="different_bucket"))

    return tuple(misses)


def _advance_tag(tag: ActiveTag | None, booking_date) -> ActiveTag | None:
    """Auto-clear `once` after one use and `duration` after its window closes."""
    if tag is None:
        return None
    if tag.mode == "once":
        return None
    if tag.is_expired(booking_date):
        return None
    return tag


def _matches_skip_pattern(patterns, txn: SourceTransaction) -> bool:
    for p in patterns:
        if p.field == "payee":
            haystack = txn.payee or ""
        elif p.field == "description":
            haystack = txn.description or ""
        else:
            continue  # narration pattern requires a matched entry; checked elsewhere
        if re.search(p.pattern, haystack, re.IGNORECASE):
            return True
    return False


def _proposal_from_outcome(outcome: MatchOutcome) -> CategoryProposal:
    """Build a categorize proposal from a `rewrite_target` matcher outcome.

    The target account comes from the matcher; metadata is folded in verbatim.
    Payee/narration are left to the existing source transaction defaults so a
    later rule or user override can still tweak them.
    """
    assert outcome.target_account is not None
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=outcome.target_account),),
        metadata=dict(outcome.metadata),
    )


def _proposal_from_rule(rule: CategorizationRule) -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=rule.target_account),),
        payee=rule.override_payee,
        narration=rule.override_narration,
        tag=rule.tag,
        rule_used=rule,
    )


def _seed_proposal(
    rule: CategorizationRule | None,
    candidates: tuple[tuple[LedgerEntry, float], ...],
) -> CategoryProposal | None:
    """The auto-built proposal that would seed Screen 1 (or replace it).

    Rule wins over candidate — a user-authored override is more authoritative
    than fuzzy-match target reuse. Returns None when neither input has
    anything to contribute (Path B: fresh pick required).
    """
    if rule is not None:
        return _proposal_from_rule(rule)
    if candidates:
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=candidates[0][0].target_account),),
        )
    return None


def _is_ambiguous_match(
    candidates: tuple[tuple[LedgerEntry, float], ...],
    min_delta: float,
) -> bool:
    """Two or more candidates with the top scores within `min_delta`.

    Single-candidate hits are unambiguous by definition. Wide gaps (top
    decisively better) are also unambiguous — the user gets Screen 1 with
    the top entry's target reused.
    """
    if len(candidates) < 2:
        return False
    return (candidates[0][1] - candidates[1][1]) < min_delta


def _silent_skip_proposal(
    rule: CategorizationRule | None,
    candidates: tuple[tuple[LedgerEntry, float], ...],
    min_delta: float,
) -> CategoryProposal | None:
    """Return the seed proposal iff invoking categorize_fn would be a no-op.

    A "no-op" means the proposal produces zero `_diff_changes` against the
    relevant entry (or every ambiguous candidate, when the top two scores
    are within `min_delta`). The user gets nothing to consent to — the
    pipeline silent-skips the row.

    Returns None when there's a real choice: either no candidate to diff
    against (a fresh entry the user must confirm), or at least one
    candidate where the proposal would actually change a field.
    """
    if not candidates:
        # No entry to diff against — even a rule-driven new entry needs
        # user consent (Enter on Screen 1).
        return None

    seed = _seed_proposal(rule, candidates)
    if seed is None:  # pragma: no cover - candidates non-empty guarantees seed
        return None

    if rule is None and _is_ambiguous_match(candidates, min_delta):
        top_score = candidates[0][1]
        diff_targets = [
            entry for (entry, score) in candidates
            if (top_score - score) < min_delta
        ]
    else:
        diff_targets = [candidates[0][0]]

    if all(not _diff_changes(entry, seed, rule) for entry in diff_targets):
        return seed
    return None


def _derive_rule(txn: SourceTransaction, proposal: CategoryProposal) -> CategorizationRule | None:
    """Synthesize a CategorizationRule from a one-off categorize proposal.

    Heuristic: prefer matching by payee when available, falling back to
    description. The pattern is the literal string anchored case-insensitively;
    users can refine it later via the rule editor.
    """
    if not proposal.postings:
        return None
    target = proposal.postings[0].account
    payee_pattern = ""
    desc_pattern = ""
    if txn.payee:
        payee_pattern = _escape_for_regex(txn.payee)
    elif txn.description:
        desc_pattern = _escape_for_regex(txn.description)
    if not payee_pattern and not desc_pattern:
        return None
    return CategorizationRule(
        target_account=target,
        payee_pattern=payee_pattern,
        description_pattern=desc_pattern,
        bank_key=txn.bank_key,
        override_payee=proposal.payee,
        override_narration=proposal.narration,
        tag=proposal.tag,
    )


def _escape_for_regex(s: str) -> str:
    return re.escape(s.strip())


def _synthesize_counter_leg(
    result: ImportResult,
    txn: SourceTransaction,
    *,
    internal_prefixes: tuple[str, ...],
    bank_accounts: set[str],
) -> LedgerEntry | None:
    """Create an inferred-amount LedgerEntry mirroring a cross-bank transfer.

    Fires when `_process_transaction` returns `action="new"` and the
    proposal's target account looks like another configured bank (or
    matches any `internal_transfer_account_prefixes`). The synthesized
    entry sits in the *target* bank's bucket so subsequent CSV rows
    from that bank match it via the scorer's `amount_inferred` path
    (rather than the user being prompted again for the same flow).

    `amount_inferred=True` is the contract that makes
    `_propose_date_metadata` fire when the second bank's CSV date
    disagrees with the first leg's booking date.
    """
    if result.action != "new" or result.proposal is None:
        return None
    target = result.proposal.target_account
    if not target:
        return None
    if target not in bank_accounts and not target.startswith(internal_prefixes):
        return None
    return LedgerEntry(
        date=txn.value_date or txn.booking_date,
        narration=result.proposal.narration or txn.description or "",
        payee=result.proposal.payee or txn.payee,
        source_account=target,
        target_account="",
        amount=-txn.amount,
        currency=txn.currency,
        amount_inferred=True,
        metadata={"_pending_in_session": "true"},
        file_path="",
        line_start=0,
    )


def _propose_date_metadata(
    txn: SourceTransaction,
    entry: LedgerEntry,
    proposal: CategoryProposal,
    *,
    paypal_account: str | None,
) -> tuple[list[ProposedChange], CategoryProposal]:
    """For amount_inferred (cross-bank transit) matches, propose date
    metadata on the matched leg's posting when the CSV and ledger
    dates disagree. Returns the (possibly empty) ProposedChange list
    plus a (possibly mutated) proposal carrying the new metadata.

    Routing:

    | CSV date vs entry.date | Matched account is `paypal_account`? | Key      |
    |  Earlier               | Yes                                  | `paypal` |
    |  Earlier               | No                                   | `actual` |
    |  Later                 | (n/a)                                | `settle` |
    |  Equal                 | (n/a)                                | (none)   |

    The metadata sits on the matched-entry's posting — the inferred
    leg, which is the one that needs the alternate-date hint so the
    user's plugin moves the posting back to the CSV's recorded date.
    The writer renders posting-level metadata indented under the
    posting line per Phase 1.
    """
    csv_date = txn.value_date or txn.booking_date
    if csv_date == entry.date:
        return [], proposal

    if csv_date > entry.date:
        key = "settle"
    elif paypal_account is not None and entry.source_account == paypal_account:
        key = "paypal"
    else:
        key = "actual"

    new_value = csv_date.isoformat()

    # The proposal's first posting is the target_account (the inferred
    # leg's account). Attach the metadata there. If the key already
    # carries the same value, no change is proposed.
    existing_value = ""
    if proposal.postings and key in proposal.postings[0].metadata:
        existing_value = proposal.postings[0].metadata[key]
    if existing_value == new_value:
        return [], proposal

    updated_postings: list[Posting] = []
    for i, p in enumerate(proposal.postings):
        if i == 0:
            updated_postings.append(
                p.model_copy(update={"metadata": {**p.metadata, key: new_value}})
            )
        else:
            updated_postings.append(p)
    updated = proposal.model_copy(update={"postings": tuple(updated_postings)})
    return (
        [ProposedChange(field=f"posting:{key}", old_val=existing_value, new_val=new_value)],
        updated,
    )


_TIMESTAMP_NARRATION_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?"
    r"\s*(Debit|Credit|Lastschrift|Gutschrift)?\s*$"
)


def _is_truncation_equivalent(proposal: str, existing: str) -> bool:
    """True when one string is a prefix of the other (after rstrip).

    The writer silently truncates narrations to `narration_max_length`;
    re-importing the same row with the original (longer) narration must
    not register as a field change. Symmetric: the existing entry might
    be the longer one if the user lowered the truncation length.
    """
    p = proposal.rstrip()
    e = existing.rstrip()
    if not p or not e:
        return False
    return p.startswith(e) or e.startswith(p)


def _diff_changes(
    entry: LedgerEntry,
    proposal: CategoryProposal,
    rule: CategorizationRule | None,
) -> list[ProposedChange]:
    """Compute the field-level delta between an existing entry and a new proposal,
    honoring per-rule suppression flags."""

    changes: list[ProposedChange] = []
    suppress_all = rule.suppress_updates if rule else False
    if suppress_all:
        return []

    if proposal.payee and proposal.payee != (entry.payee or ""):
        if not (rule and rule.suppress_payee_updates):
            changes.append(ProposedChange("payee", entry.payee or "", proposal.payee))

    if proposal.narration and proposal.narration != entry.narration:
        # A1/A8: a re-imported CSV row whose narration was previously
        # truncated at write time differs by suffix only. Don't propose
        # rewinding the truncation as a field change.
        if not _is_truncation_equivalent(proposal.narration, entry.narration):
            # A2: bare CSV `Type`-shaped values (timestamp + Debit/Credit)
            # are pure transport metadata, not anything the user typed.
            # Suppress the overwrite when the existing entry already has
            # a real narration ("Burger King") and no rule forces it.
            timestamp_proposal = bool(
                _TIMESTAMP_NARRATION_RE.match(proposal.narration)
            )
            real_existing = bool(
                entry.narration and not _TIMESTAMP_NARRATION_RE.match(entry.narration)
            )
            if not (timestamp_proposal and real_existing and rule is None):
                if not (rule and rule.suppress_narration_updates):
                    changes.append(
                        ProposedChange("narration", entry.narration, proposal.narration)
                    )

    if proposal.target_account and proposal.target_account != entry.target_account:
        # A4: salary / multi-leg entries are user-authored structures; a
        # single CSV row should not rewrite the merchant-side account
        # away from whatever the user spread across the deduction legs.
        if entry.has_multiple_postings:
            return changes
        if not (rule and rule.suppress_account_updates):
            changes.append(
                ProposedChange("account", entry.target_account, proposal.target_account)
            )

    return changes


def _format_new_entry(
    bank: BankConfig,
    txn: SourceTransaction,
    proposal: CategoryProposal,
    narration_max_length: int | None = None,
) -> str:
    """Render a new beancount transaction text from the proposal."""
    payee = proposal.payee or txn.payee
    narration = proposal.narration or txn.description or ""

    postings: list[tuple[str, str | None, dict[str, str]]] = []
    # Source-account leg always carries the explicit amount + currency.
    postings.append(
        (bank.account, f"{txn.amount} {txn.currency}", {})
    )
    for p in proposal.postings:
        amount_str: str | None = None
        if p.amount is not None:
            currency = p.currency or txn.currency
            amount_str = f"{p.amount} {currency}"
        postings.append((p.account, amount_str, dict(p.metadata)))

    metadata = dict(proposal.metadata)
    if proposal.tag:
        metadata["tag"] = proposal.tag

    return format_transaction(
        date_str=txn.booking_date.isoformat(),
        flag="*",
        payee=payee,
        narration=narration,
        postings=postings,
        metadata=metadata,
        narration_max_length=narration_max_length,
    )

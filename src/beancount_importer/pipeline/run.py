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
from dataclasses import dataclass, replace
from pathlib import Path

from beancount_importer.config import BankConfig
from beancount_importer.matching.dedup import find_definitive_duplicate
from beancount_importer.matching.registry import (
    MatcherHook,
    first_outcome,
    load_matchers,
)
from beancount_importer.matching.scorer import find_candidates
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    SourceTransaction,
)
from beancount_importer.pipeline._clean import (
    clean_acquirer_prefix,
    clean_paypal_noise,
    clean_spk_transfer_prefix,
)
from beancount_importer.pipeline._merge import _apply_merge_decision
from beancount_importer.pipeline._paypal_bundles import resolve_paypal_settlements
from beancount_importer.pipeline._proposal import (
    _derive_rule,
    _is_ambiguous_match,
    _proposal_from_outcome,
    _proposal_from_rule,
    _seed_proposal,
    _silent_skip_proposal,
)
from beancount_importer.pipeline._result import (
    _build_result,
    _fold_inflight_date_hint,
    _link_placeholder_result,
    _synthesize_counter_leg,
)
from beancount_importer.pipeline._shared import (
    _load_account_chart,
    _load_all_outputs,
    _parse_all_inputs,
    _select_banks,
)
from beancount_importer.pipeline.types import (
    CategorizeContext,
    CategorizeFn,
    MergeContext,
    MergeFn,
    NearMiss,
    Reporter,
)
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.engine import find_matching_rule
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import (
    ActiveTag,
    RememberedTag,
    TagStateDelta,
    known_tags,
    remember,
)
from beancount_importer.session import ImportSession
from beancount_importer.transforms import apply_transforms, load_transforms
from beancount_importer.transforms.steam import SteamEnricher, load_steam_index


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
    # Collapse PayPal pass-through funding rows ("Bank Deposit"-style deposits
    # that fund a same-CSV purchase) into that purchase, so the pair books as a
    # single settle_inv-shaped entry instead of a purchase + a separate
    # transfer. Needs the rules (the CSV never names the funding bank), so it
    # runs here rather than in `_parse_all_inputs`.
    inputs = resolve_paypal_settlements(
        inputs,
        list(session.rules),
        internal_prefixes=tuple(config.matching.internal_transfer_account_prefixes),
        paypal_account=config.paypal_account,
        paypal_credit_account=config.paypal_credit_account,
    )
    if session.options.chronological:
        # Stable sort: within a day, the existing bank/CSV order is the
        # tiebreak. Reordering shifts which leg of a same-session transfer
        # is processed first (toward the earlier-dated leg) but does not
        # break the leg-1→leg-2 placeholder pairing below.
        inputs = sorted(inputs, key=lambda t: t.booking_date)

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

    # The authoritative account chart from `open` directives — includes
    # accounts opened but not yet used, which the postings sweep can't see.
    account_chart = _load_account_chart(base_dir, session)

    bank_account_by_key = {b.key: b.account for b in banks}
    bank_by_key = {b.key: b for b in banks}

    transforms = load_transforms(config.transforms.enabled)
    if config.steam_history_file is not None:
        transforms = [
            *transforms,
            SteamEnricher(load_steam_index(base_dir / config.steam_history_file)),
        ]
    matchers = load_matchers(list(config.matching.enabled_matchers))
    working_rules: list[CategorizationRule] = list(session.rules)
    working_tag: ActiveTag | None = session.tag_state.active
    # Picker source for the `[t]` menu: persisted interacted tags (with their
    # windows) unioned with every tag already written to the ledger. Grown
    # in-session as the user sets new tags, so later transactions can re-pick
    # them without re-typing.
    ledger_tag_names = {t for e in existing for t in e.tags}
    working_known = known_tags(session.tag_state.recent, ledger_tag_names)

    # Pre-bucket all CSV rows by bank key so cross-source matchers can scan
    # the PayPal CSV (or any other bank) without re-iterating `inputs` per call.
    csv_by_bank: dict[str, list[SourceTransaction]] = {}
    for t in inputs:
        csv_by_bank.setdefault(t.bank_key, []).append(t)

    total = len(inputs)
    results: list[ImportResult] = (
        results_accumulator if results_accumulator is not None else []
    )
    # Maps an in-session counter-leg placeholder (by identity) to the index of
    # the leg-1 result it mirrors, so a later counter-party row's date hint can
    # be folded back onto leg 1 instead of spliced into the (location-less)
    # placeholder. See `_synthesize_counter_leg` / `_fold_inflight_date_hint`.
    counter_origin: dict[int, int] = {}

    for progress, txn in enumerate(inputs, start=1):
        reporter.on_progress(progress, total, txn.bank_key, txn.booking_date)
        # Pre-match cleaner: tidies SPK-style PayPal rows so the
        # merge-prompt display and text-similarity scoring see a
        # clean payee + description instead of bank transport noise.
        # Other rows pass through unchanged.
        txn = clean_paypal_noise(txn)
        # Strip card-acquirer descriptors ("SumUp  *Merchant", "SQ *Merchant")
        # so the written payee and matching see the real merchant, not the PSP.
        txn = clean_acquirer_prefix(txn)
        # Strip SPK's "DATUM <date>, <time> UHR <TYPE>" narration prefix that
        # duplicates the booking date and Buchungstext on online transfers.
        txn = clean_spk_transfer_prefix(txn)

        bank_cfg = bank_by_key[txn.bank_key]
        bucket = existing_by_account.get(bank_account_by_key[txn.bank_key], [])
        result, working_tag, working_rules = _process_transaction(
            txn=txn,
            bank=bank_cfg,
            existing=bucket,
            existing_all=existing,
            account_chart=account_chart,
            csv_by_bank=csv_by_bank,
            matchers=matchers,
            config=config,
            working_rules=working_rules,
            working_tag=working_tag,
            working_known=working_known,
            transforms_hooks=transforms,
            categorize_fn=categorize_fn,
            merge_fn=merge_fn,
            decisions=decisions,
            auto_threshold=session.options.auto_threshold,
            progress=(progress, total),
        )

        # Grow the in-session picker list when the user just set a tag, so the
        # next transaction can re-pick it (and its window) without re-typing.
        if (
            (delta := result.tag_state_delta) is not None
            and delta.op == "set"
            and delta.new_state is not None
        ):
            working_known = remember(working_known, delta.new_state)

        # Leg 2 of a same-session transfer: it matched an in-session placeholder
        # that has no file to splice into. Redirect its date hint onto leg 1's
        # pending entry and drop the placeholder splice so persistence skips it.
        matched = result.matched_entry
        if (
            result.action == "update"
            and matched is not None
            and matched.metadata.get("_pending_in_session") == "true"
            and id(matched) in counter_origin
            and result.proposed_changes
        ):
            leg1_idx = counter_origin[id(matched)]
            leg1 = results[leg1_idx]
            results[leg1_idx] = _fold_inflight_date_hint(
                leg1,
                matched.source_account,
                list(result.proposed_changes),
                bank_by_key[leg1.source_txn.bank_key],
                narration_max_length=config.narration_max_length,
            )
            result = result.model_copy(update={"proposed_changes": []})

        counter = _synthesize_counter_leg(
            result,
            txn,
            internal_prefixes=tuple(config.matching.internal_transfer_account_prefixes),
            bank_accounts=set(bank_accounts),
        )
        if counter is not None:
            existing_by_account.setdefault(counter.source_account, []).append(counter)
            existing.append(counter)
            # leg 1 (`result`) is appended below at index len(results).
            counter_origin[id(counter)] = len(results)

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
    working_known: tuple[RememberedTag, ...] = ()

    rule: CategorizationRule | None = None
    match_txn: SourceTransaction | None = None
    candidates: tuple[tuple[LedgerEntry, float], ...] = ()
    matcher_proposal: CategoryProposal | None = None
    proposal: CategoryProposal | None = None
    new_rule: CategorizationRule | None = None
    replaced_rule: CategorizationRule | None = None
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
    account_chart: tuple[str, ...],
    csv_by_bank: dict[str, list[SourceTransaction]],
    matchers: list[MatcherHook],
    config,  # Config; type omitted to avoid a circular import at type level
    working_rules: list[CategorizationRule],
    working_tag: ActiveTag | None,
    working_known: tuple[RememberedTag, ...],
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
        working_known=working_known,
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
        account_chart=account_chart,
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
        replaced_rule=state.replaced_rule,
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
    own previously-written entry and gets re-prompted. A matched rule also
    takes precedence over the replay log (`_try_replay` bows out when
    `state.rule` is set).
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

    Priority is matches > rules > replay: an already-booked row (dedup),
    cross-source match, or matching rule all take precedence, and the replay
    log is the lowest-priority fallback — consulted only for a row none of
    those handle.
    """
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

    matched = _try_matcher(
        state,
        matchers=matchers,
        csv_by_bank=csv_by_bank,
        existing=existing,
        existing_all=existing_all,
    )
    if isinstance(matched, ImportResult):
        return matched

    replay = _try_replay(matched, decisions=decisions, existing=existing, config=config)
    if replay is not None:
        return replay
    return matched


def _try_replay(
    state: _TxnState,
    *,
    decisions: DecisionLog,
    existing: list[LedgerEntry],
    config,
) -> ImportResult | None:
    """If a past decision matches this txn, reproduce its result.

    Skipped when a rule matched or a cross-source matcher fired — those are
    stronger, current signals; replay only fills the gap for a row nothing
    else claims.
    """
    if state.rule is not None or state.matcher_proposal is not None:
        return None
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
    if outcome.kind == "link_placeholder":
        matched = outcome.matched_entry
        if matched is None or matched.source_account == state.bank.account:
            # Protocol requires the entry. And a row can never settle a
            # placeholder on its *own* bank — that shape is the funding row
            # itself (a re-import that slipped past dedup), not the
            # PayPal-side purchase; linking it would stamp the bank date as
            # the PayPal date and pre-settle the flow.
            return state
        # The cross-bank gate above means only the global view holds the
        # placeholder — the row's own bucket can't.
        existing_all.remove(matched)
        return _link_placeholder_result(state.txn, matched)
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
    account_chart: tuple[str, ...],
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
        state.rule,
        state.candidates,
        config.matching.min_delta,
        txn=state.txn,
        paypal_account=config.paypal_account,
    )
    if silent is not None:
        return state.evolve(proposal=silent)

    from beancount_importer.matching.account_suggest import (
        is_self_transfer,
        rank_accounts,
    )

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
        known_tags=state.working_known,
        existing_entries=tuple(existing_all),
        known_accounts=account_chart,
        own_account_prefixes=(
            tuple(config.matching.internal_transfer_account_prefixes)
            if is_self_transfer(state.txn.payee, config.owner_names)
            else ()
        ),
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
    # A rule edited in the interactive editor is used verbatim; otherwise
    # auto-derive from the txn (replay / non-interactive save-as-rule).
    new_rule = state.proposal.pending_rule or _derive_rule(state.txn, state.proposal)
    if new_rule is None:
        return state
    replaced = state.proposal.replaces_rule
    if replaced is not None:
        # Editing a matched rule: swap it in place so subsequent txns this
        # session use the edited version, and flag the result so persistence
        # replaces rather than appends.
        working = tuple(
            new_rule if r == replaced else r for r in state.working_rules
        )
        return state.evolve(
            new_rule=new_rule, replaced_rule=replaced, working_rules=working
        )
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





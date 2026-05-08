"""Pipeline: turn source transactions into ImportResults.

The pipeline is deterministic given inputs. It does no I/O beyond reading CSV
sources, reading existing ledger files, and (via DecisionLog) appending one
JSONL line per non-trivial decision. It does NOT write the ledger — the CLI
collects ImportResults and applies splices/appends after the run.

Iteration-local mutable state (working rules and active tag) lives only for
one `run()` call. The session itself is frozen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from beancount_importer.beancount_io.reader import read_ledger_multi
from beancount_importer.beancount_io.writer import format_transaction
from beancount_importer.config import BankConfig
from beancount_importer.matching.dedup import is_duplicate
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
from beancount_importer.parsers.base import Parser
from beancount_importer.parsers.generic import GenericCsvParser
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.engine import find_matching_rule
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag, TagStateDelta
from beancount_importer.session import ImportSession
from beancount_importer.transforms import apply_transforms, load_transforms


# ── Public types ──────────────────────────────────────────────────────────────


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


# Ledger files whose contents are infrastructure (open/balance/pad assertions
# and similar), not real transactions. Always excluded from the global outputs
# sweep — even when a wrapper file like `main.bean` includes them, the
# resulting entries are filtered out by source path so they don't double-count
# in stats or surface as "no CSV source".
_EXCLUDED_LEDGER_FILES = frozenset({"setup.bean"})


def _build_parser(bank: BankConfig) -> Parser:
    # GenericCsvParser is the only built-in shape today. parser_class plumbing
    # is intentionally deferred until we have a real custom parser to register.
    return GenericCsvParser(bank)


def _load_all_outputs(
    base_dir: Path,
    transactions_dir: str,
    *,
    account_prefixes: Iterable[str],
    extra_accounts: Iterable[str] = (),
    metadata_date_keys: Iterable[str] = (),
    synthesize_from_metadata: dict[str, str] | None = None,
) -> list[LedgerEntry]:
    """Sweep `transactions_dir/**/*.bean` and return every bank-shaped entry.

    A posting is in scope when its account is in `extra_accounts` or starts
    with one of `account_prefixes`. Each in-scope posting becomes one
    `LedgerEntry`, so an SPK→PayPal transfer yields two entries (one per leg).

    Reads are parallelized over a small thread pool — `beancount.loader.load_file`
    dominates the wall-clock and releases the GIL inside its C parser.
    `setup.bean` files are excluded both at the path level and on the
    `entry.file_path` to handle entries reached via `include` wrappers.

    Dedup by `(file_path, line_start, source_account)` collapses duplicates
    surfacing through `include` wrappers without conflating distinct legs of
    a cross-bank transfer (which carry the same line but different accounts).
    """
    tx_root = (base_dir / transactions_dir).resolve()
    paths = sorted(
        p for p in tx_root.rglob("*.bean") if p.name not in _EXCLUDED_LEDGER_FILES
    ) if tx_root.exists() else []
    if not paths:
        return []

    prefixes = tuple(account_prefixes)
    accounts = tuple(extra_accounts)
    date_keys = tuple(metadata_date_keys)
    synth_map = dict(synthesize_from_metadata or {})

    def _read(path: Path) -> list[LedgerEntry]:
        return read_ledger_multi(
            path,
            accounts=accounts,
            account_prefixes=prefixes,
            metadata_date_keys=date_keys,
            synthesize_from_metadata=synth_map,
        )

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        per_path = list(pool.map(_read, paths))

    deduped: dict[tuple[str, int, str], LedgerEntry] = {}
    for entries in per_path:
        for entry in entries:
            if Path(entry.file_path).name in _EXCLUDED_LEDGER_FILES:
                continue
            deduped.setdefault(
                (entry.file_path, entry.line_start, entry.source_account), entry
            )
    return list(deduped.values())


def _gather_csv_files(bank: BankConfig, base_dir: Path) -> list[Path]:
    # Bank exports vary in case (`SPK_2024.CSV` vs `n26-*.csv`); macOS / Linux
    # are case-sensitive by default but the pattern in the config may not
    # match the on-disk casing. Python's pathlib supports an opt-out as of 3.12.
    return sorted(base_dir.glob(bank.file_glob, case_sensitive=False))


def _select_banks(session: ImportSession) -> list[BankConfig]:
    bank_filter = session.options.bank_filter
    if not bank_filter:
        return list(session.config.banks)
    return [b for b in session.config.banks if b.key == bank_filter]


def _parse_all_inputs(
    banks: list[BankConfig],
    base_dir: Path,
    year_filter: tuple[int, ...] | None,
    reporter: Reporter | None,
) -> list[SourceTransaction]:
    """Parse every CSV across `banks` into a flat list, in deterministic order.

    A `None` reporter swallows parse errors silently — used by the preview
    path so a malformed CSV doesn't crash the report. With a reporter, errors
    surface via `reporter.on_error`.
    """
    flat: list[SourceTransaction] = []
    allowed = set(year_filter) if year_filter is not None else None
    for bank in banks:
        parser = _build_parser(bank)
        for csv_file in _gather_csv_files(bank, base_dir):
            try:
                rows = list(parser.parse(str(csv_file)))
            except Exception as exc:
                if reporter is not None:
                    reporter.on_error(f"{bank.key}: failed to parse {csv_file}: {exc}")
                continue
            if allowed is not None:
                rows = [t for t in rows if t.booking_date.year in allowed]
            flat.extend(rows)
    return flat


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

        decisions.record(txn, result)
        reporter.on_result(result)
        results.append(result)

        if result.action == "quit":
            break

    return results


# ── Per-transaction processing ────────────────────────────────────────────────


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
    """Process one txn, returning the result + updated tag + updated rules list."""

    # 1. Replay log takes precedence over everything else.
    replayed = decisions.lookup(txn)
    if replayed is not None:
        result = _build_result(
            txn=txn,
            bank=bank,
            proposal=replayed,
            existing=existing,
            matched_rule=None,
            is_replay=True,
            new_rule=None,
            tag_state_delta=None,
            min_score=config.matching.min_score,
        )
        return result, _advance_tag(working_tag, txn.booking_date), working_rules

    # 2. Drop confirmed duplicates entirely.
    if is_duplicate(txn, existing):
        return (
            ImportResult(
                source_txn=txn, action="skip", proposal=None, skip_reason="duplicate"
            ),
            _advance_tag(working_tag, txn.booking_date),
            working_rules,
        )

    # 3. Hard suppression — skip_update_patterns drop the proposal entirely.
    if _matches_skip_pattern(config.skip_update_patterns, txn):
        return (
            ImportResult(
                source_txn=txn, action="skip", proposal=None, skip_reason="skip_rule"
            ),
            _advance_tag(working_tag, txn.booking_date),
            working_rules,
        )

    # 4. Cross-source matchers: detect rows already booked elsewhere (skip)
    # or rows that should book to a non-default account (rewrite_target). The
    # matcher uses the full CSV+ledger universe, not just the current bank's
    # bucket. Recorded as a proposal so replay reproduces the outcome without
    # re-running matchers.
    matcher_outcome = first_outcome(matchers, txn, csv_by_bank, existing_all)
    matcher_proposal: CategoryProposal | None = None
    if matcher_outcome is not None:
        if matcher_outcome.kind == "skip":
            return (
                ImportResult(
                    source_txn=txn,
                    action="skip",
                    proposal=None,
                    skip_reason="cross_source_match",
                    matched_entry=matcher_outcome.matched_entry,
                ),
                _advance_tag(working_tag, txn.booking_date),
                working_rules,
            )
        # rewrite_target: synthesize a proposal that bypasses the categorizer.
        matcher_proposal = _proposal_from_outcome(matcher_outcome, txn)

    # 5. Find rule + ranked candidates.
    rule = find_matching_rule(txn, working_rules)
    candidates = find_candidates(
        txn, existing, min_score=config.matching.min_score
    )

    # 6. Choose the proposal source. Matcher rewrites are authoritative — they
    # represent ground truth from cross-source data and pre-empt both the
    # auto-threshold path and the user prompt.
    proposal: CategoryProposal
    if matcher_proposal is not None:
        proposal = matcher_proposal
    elif (
        auto_threshold is not None
        and candidates
        and candidates[0][1] >= auto_threshold
        and rule is not None
    ):
        proposal = _proposal_from_rule(rule)
    else:
        from beancount_importer.matching.account_suggest import rank_accounts

        suggested = rule.target_account if rule is not None else None
        hints, _ = rank_accounts(
            txn, candidates, existing_all, suggested_target=suggested
        )
        context = CategorizeContext(
            txn=txn,
            rules=tuple(working_rules),
            candidates=tuple(candidates),
            matched_rule=rule,
            account_hints=tuple(hints),
            active_tag=working_tag,
            existing_entries=tuple(existing_all),
            source_account=bank.account,
            progress=progress,
        )
        proposal = categorize_fn(context)

    # 6. Apply transforms only when there's a rule to drive them.
    if rule is not None:
        proposal = apply_transforms(transforms_hooks, proposal, txn, rule)

    # 6b. Apply a user-driven tag-state delta (Screen 1's `[t]` hotkey).
    # The proposal arrives with `tag_state_delta` set; we mutate working_tag
    # before the auto-stamp step so this txn picks up the new tag. The
    # delta also surfaces on the result (step 9) for persistence.
    user_tag_delta = proposal.tag_state_delta
    if user_tag_delta is not None:
        if user_tag_delta.op == "set":
            working_tag = user_tag_delta.new_state
        elif user_tag_delta.op == "clear":
            working_tag = None

    # 7. Apply active tag (if any and applicable) when proposal didn't set one.
    if working_tag is not None and proposal.tag is None and working_tag.applies_to(txn.booking_date):
        proposal = proposal.model_copy(update={"tag": working_tag.tag})

    # 8. Synthesize a rule from the proposal if the user asked to save it.
    new_rule: CategorizationRule | None = None
    new_rules_list = working_rules
    if proposal.save_as_rule and proposal.action == "categorize":
        new_rule = _derive_rule(txn, proposal)
        if new_rule is not None:
            new_rules_list = [*working_rules, new_rule]

    # 9. Compute tag-state delta. User intent dominates: a user `set`/`clear`
    # is recorded verbatim. Otherwise we fall back to the auto-clear that
    # fires when `once` mode expires or `duration` runs past `until_date`.
    next_tag = _advance_tag(working_tag, txn.booking_date)
    tag_delta: TagStateDelta | None = None
    if user_tag_delta is not None:
        tag_delta = user_tag_delta
    elif next_tag != working_tag and working_tag is not None:
        tag_delta = TagStateDelta(op="clear")

    result = _build_result(
        txn=txn,
        bank=bank,
        proposal=proposal,
        existing=existing,
        matched_rule=rule,
        is_replay=False,
        new_rule=new_rule,
        tag_state_delta=tag_delta,
        min_score=config.matching.min_score,
    )

    # 10. Merge prompt — fires only when the auto-decision would change
    # an existing entry. The host (cli.py) renders Screen 3 and returns
    # one of six outcomes; the helper rewrites the result accordingly.
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
                active_tag=working_tag,
            )
        )
        result, new_rules_list = _apply_merge_decision(
            result, merge_decision, txn, bank, working_rules
        )
    return result, next_tag, new_rules_list


def _apply_merge_decision(
    result: ImportResult,
    decision: MergeDecision,
    txn: SourceTransaction,
    bank: BankConfig,
    working_rules: list[CategorizationRule],
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
        new_text = _format_new_entry(bank, txn, result.proposal)
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
) -> ImportResult:
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
    candidates = find_candidates(txn, existing, min_score=min_score)
    best: LedgerEntry | None = candidates[0][0] if candidates else None
    action = "update" if best is not None else "new"

    proposed_changes: list[ProposedChange] = []
    new_entry_text = ""
    if best is not None:
        proposed_changes = _diff_changes(best, proposal, matched_rule)
    else:
        new_entry_text = _format_new_entry(bank, txn, proposal)

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
    import re

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


def _proposal_from_outcome(
    outcome: MatchOutcome, txn: SourceTransaction
) -> CategoryProposal:
    """Build a categorize proposal from a `rewrite_target` matcher outcome.

    The target account comes from the matcher; metadata is folded in verbatim.
    Payee/narration are left to the existing source transaction defaults so a
    later rule or user override can still tweak them.
    """
    del txn  # currently unused; kept for symmetry with `_proposal_from_rule`
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
    import re

    return re.escape(s.strip())


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

    # Cross-bank transit match: the matched entry lives in another bank's
    # ledger (e.g., the PayPal leg of an SPK→PayPal transfer). The CSV row
    # being imported describes the *merchant* side; the existing entry
    # describes the *funding* side. Updating the funding-bank's payee or
    # narration from a PayPal-CSV proposal is wrong — it'd corrupt the
    # original SPK booking. Treat the row as already accounted for.
    if entry.amount_inferred:
        return []

    if proposal.payee and proposal.payee != (entry.payee or ""):
        if not (rule and rule.suppress_payee_updates):
            changes.append(ProposedChange("payee", entry.payee or "", proposal.payee))

    if proposal.narration and proposal.narration != entry.narration:
        if not (rule and rule.suppress_narration_updates):
            changes.append(
                ProposedChange("narration", entry.narration, proposal.narration)
            )

    if proposal.target_account and proposal.target_account != entry.target_account:
        if not (rule and rule.suppress_account_updates):
            changes.append(
                ProposedChange("account", entry.target_account, proposal.target_account)
            )

    return changes


def _format_new_entry(
    bank: BankConfig,
    txn: SourceTransaction,
    proposal: CategoryProposal,
) -> str:
    """Render a new beancount transaction text from the proposal."""
    payee = proposal.payee or txn.payee
    narration = proposal.narration or txn.description or ""

    postings: list[tuple[str, str | None]] = []
    # Source-account leg always carries the explicit amount + currency.
    postings.append(
        (bank.account, f"{txn.amount} {txn.currency}")
    )
    for p in proposal.postings:
        amount_str: str | None = None
        if p.amount is not None:
            currency = p.currency or txn.currency
            amount_str = f"{p.amount} {currency}"
        postings.append((p.account, amount_str))

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
    )


# ── Preview-only: reverse provenance stats ────────────────────────────────────


@dataclass(frozen=True)
class BeanProvenanceStats:
    """Reverse-matching counts for one section in one year.

    `section` is either a configured bank's account (`Assets:B:SPK`) or the
    relative path of the `.bean` file that holds non-configured entries
    (e.g. `TR.bean`). File grouping collapses per-sub-account noise so a
    user with one TR sub-account per share sees one TR section, not many.
    Counts are deduped per Transaction; `bean_expanded` is bean-query's
    post-plugin count, populated only for configured-bank sections.
    """

    section: str
    year: int
    total_in_bean: int = 0
    bean_unmatched: int = 0
    bean_expanded: int = 0


def _amount_cents(amount) -> int:
    return int(round(float(amount) * 100))


def _has_csv_match(
    entry: LedgerEntry,
    csv_txns: list[SourceTransaction],
    tolerance_days: int = 5,
) -> bool:
    """Mirror of the reference's bean-vs-csv reverse match: amount cents
    must agree exactly, booking date within `tolerance_days`. Also accepts
    matches against any of the entry's `metadata_dates` (settle/paypal/actual)
    so plugin-rewritten dates don't show up as unmatched."""
    target_cents = _amount_cents(entry.amount)
    candidate_dates = (entry.date, *entry.metadata_dates)
    for csv_txn in csv_txns:
        if _amount_cents(csv_txn.amount) != target_cents:
            continue
        for d in candidate_dates:
            if abs((csv_txn.booking_date - d).days) <= tolerance_days:
                return True
    return False


def compute_bean_provenance_stats(
    session: ImportSession,
    base_dir: Path,
) -> dict[tuple[str, int], BeanProvenanceStats]:
    """Per-(section, year) bean-side stats for preview and post-import summary.

    Reads every CSV across configured banks (the input universe) and every
    `.bean` under `transactions_dir` (the output universe), then matches the
    two by amount + date for each existing entry. Sections are:

    - Each configured bank's account (`Assets:B:SPK`, …): one section per bank.
    - Each `.bean` file containing entries whose accounts aren't tied to any
      configured bank: one section per file, keyed by relative path.

    Plus a year-aggregate sentinel `("", year)` that counts unique ledger
    transactions across the year (deduped across cross-account legs).
    """
    config = session.config
    year_filter = session.options.year_filter
    banks = _select_banks(session)

    all_csv = _parse_all_inputs(banks, base_dir, year_filter, reporter=None)
    existing = _load_all_outputs(
        base_dir,
        config.transactions_dir,
        account_prefixes=tuple(config.matching.internal_transfer_account_prefixes),
        extra_accounts=tuple({b.account for b in banks}),
        metadata_date_keys=tuple(config.matching.metadata_date_keys),
        synthesize_from_metadata=dict(config.matching.synthesize_from_metadata),
    )

    bank_accounts = {b.account for b in banks}
    tx_root = (base_dir / config.transactions_dir).resolve()
    expanded = _expanded_counts(config, base_dir, banks, year_filter)

    # Per-section, per-year buckets dedupe by (file_path, line_start) so a
    # single transaction with multiple in-scope postings (e.g. a TR rebalance
    # touching two sub-accounts) counts once. The same key underpins the
    # cross-section year-aggregate at ("", year).
    by_section: dict[tuple[str, int], dict[tuple[str, int], LedgerEntry]] = {}
    year_unique: dict[int, dict[tuple[str, int], LedgerEntry]] = {}

    for entry in existing:
        year = entry.date.year
        if year_filter is not None and year not in year_filter:
            continue
        if entry.source_account in bank_accounts:
            section = entry.source_account
        else:
            section = str(Path(entry.file_path).resolve().relative_to(tx_root))
        line_key = (entry.file_path, entry.line_start)
        by_section.setdefault((section, year), {}).setdefault(line_key, entry)
        year_unique.setdefault(year, {}).setdefault(line_key, entry)

    def _stats(section: str, year: int, entries: list[LedgerEntry]) -> BeanProvenanceStats:
        return BeanProvenanceStats(
            section=section,
            year=year,
            total_in_bean=len(entries),
            bean_unmatched=sum(1 for e in entries if not _has_csv_match(e, all_csv)),
            bean_expanded=expanded.get((section, year), 0),
        )

    stats: dict[tuple[str, int], BeanProvenanceStats] = {
        (section, year): _stats(section, year, list(unique.values()))
        for (section, year), unique in by_section.items()
    }
    for year, unique in year_unique.items():
        stats[("", year)] = _stats("", year, list(unique.values()))
    return stats


def _expanded_counts(
    config,
    base_dir: Path,
    banks: list[BankConfig],
    year_filter: tuple[int, ...] | None,
) -> dict[tuple[str, int], int]:
    """Run `bean-query` per (account, year) to get the plugin-expanded count.

    Returns `{}` when `main_bean` is unset, no per-year file exists, or
    `bean-query` isn't on PATH. Failures per call are swallowed — this is a
    nice-to-have preview line, not load-bearing.
    """
    main_bean_template: str | None = getattr(config, "main_bean", None)
    if not main_bean_template or year_filter is None:
        return {}
    if shutil.which("bean-query") is None:
        return {}

    jobs: list[tuple[str, int, Path]] = []
    for year in year_filter:
        main_bean_path = (base_dir / main_bean_template.format(year=year)).resolve()
        if main_bean_path.exists():
            jobs.extend((bank.account, year, main_bean_path) for bank in banks)
    if not jobs:
        return {}

    def _run_one(job: tuple[str, int, Path]) -> tuple[str, int, int] | None:
        account, year, main_bean_path = job
        query = (
            f"SELECT count(date) WHERE date >= {year}-01-01"
            f" AND date < {year + 1}-01-01"
            f" AND account ~ '{account}'"
        )
        try:
            result = subprocess.run(
                ["bean-query", str(main_bean_path), query],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                return account, year, int(stripped)
        return None

    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        outcomes = list(pool.map(_run_one, jobs))
    return {(account, year): n for o in outcomes if o is not None for account, year, n in [o]}

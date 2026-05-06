"""Pipeline: turn source transactions into ImportResults.

The pipeline is deterministic given inputs. It does no I/O beyond reading CSV
sources, reading existing ledger files, and (via DecisionLog) appending one
JSONL line per non-trivial decision. It does NOT write the ledger — the CLI
collects ImportResults and applies splices/appends after the run.

Iteration-local mutable state (working rules and active tag) lives only for
one `run()` call. The session itself is frozen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from beancount_importer.beancount_io.reader import read_ledger
from beancount_importer.beancount_io.writer import format_transaction
from beancount_importer.config import BankConfig
from beancount_importer.matching.dedup import is_duplicate
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
    """Inputs supplied to a CategorizeFn for one transaction."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    txn: SourceTransaction
    rules: tuple[CategorizationRule, ...]
    candidates: tuple[tuple[LedgerEntry, float], ...] = ()
    matched_rule: CategorizationRule | None = None
    account_hints: tuple[str, ...] = ()
    active_tag: ActiveTag | None = None


CategorizeFn = Callable[[CategorizeContext], CategoryProposal]


@runtime_checkable
class Reporter(Protocol):
    """Receives all user-visible output from the pipeline."""

    def on_result(self, result: ImportResult) -> None: ...
    def on_progress(self, current: int, total: int, bank: str) -> None: ...
    def on_warning(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...


class NoopReporter:
    """Discard all events; useful in tests."""

    def on_result(self, result: ImportResult) -> None:
        del result

    def on_progress(self, current: int, total: int, bank: str) -> None:
        del current, total, bank

    def on_warning(self, message: str) -> None:
        del message

    def on_error(self, message: str) -> None:
        del message


# ── Pipeline ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _BankWork:
    bank: BankConfig
    parser: Parser
    transactions: list[SourceTransaction]
    existing_entries: list[LedgerEntry]


def _build_parser(bank: BankConfig) -> Parser:
    # GenericCsvParser is the only built-in shape today. parser_class plumbing
    # is intentionally deferred until we have a real custom parser to register.
    return GenericCsvParser(bank)


def _resolve(base_dir: Path, template: str, year: int) -> Path:
    return (base_dir / template.format(year=year)).resolve()


def _load_existing(
    bank: BankConfig,
    base_dir: Path,
    years: tuple[int, ...] | None,
    transactions_dir: str,
    metadata_date_keys: tuple[str, ...] = (),
    synthesize_from_metadata: dict[str, str] | None = None,
) -> list[LedgerEntry]:
    """Load every already-imported entry for `bank`, across all years.

    Dedup needs to see the full ledger: CSVs straddle year boundaries (a 2024
    Sparkasse export still includes Q4 2023), so loading only one year's
    output_file would resurface real duplicates as "new" entries.

    Two scan paths:
    1. The bank's `source_files` template, expanded for each year in the
       active filter — the conventional one-file-per-bank-per-year layout.
       Skipped when `years` is None (all-years mode); the rglob below
       already sweeps every year-folder under transactions_dir.
    2. `transactions_dir/**/*.bean` — catches mixed-bank monthly files
       (`2022-01.bean`, etc.) and historical years.
    """
    seen_paths: set[Path] = set()
    entries: list[LedgerEntry] = []
    read_kwargs: dict = {}
    if metadata_date_keys:
        read_kwargs["metadata_date_keys"] = metadata_date_keys
    if synthesize_from_metadata:
        read_kwargs["synthesize_from_metadata"] = synthesize_from_metadata

    if years:
        for year in years:
            for src in bank.source_files:
                path = _resolve(base_dir, src, year)
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                if path.exists():
                    entries.extend(read_ledger(path, bank.account, **read_kwargs))

    tx_root = (base_dir / transactions_dir).resolve()
    if tx_root.exists():
        for bean in tx_root.rglob("*.bean"):
            if bean in seen_paths:
                continue
            seen_paths.add(bean)
            entries.extend(read_ledger(bean, bank.account, **read_kwargs))

    return entries


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


def run(
    session: ImportSession,
    base_dir: Path,
    categorize_fn: CategorizeFn,
    reporter: Reporter,
    decisions: DecisionLog | None = None,
) -> list[ImportResult]:
    """Execute the import pipeline.

    `base_dir` is the directory used to resolve all relative paths in the config
    (typically the directory containing `import_config.toml`). The session's
    `options.year_filter` (if any) scopes which transactions are processed;
    output paths are resolved against each transaction's own booking year by
    the CLI after the pipeline returns.
    """
    decisions = decisions if decisions is not None else DecisionLog(None)
    config = session.config
    year_filter = session.options.year_filter

    banks = _select_banks(session)
    work: list[_BankWork] = []
    for bank in banks:
        parser = _build_parser(bank)
        txns: list[SourceTransaction] = []
        for csv_file in _gather_csv_files(bank, base_dir):
            try:
                txns.extend(parser.parse(str(csv_file)))
            except Exception as exc:
                reporter.on_error(f"{bank.key}: failed to parse {csv_file}: {exc}")
        if year_filter is not None:
            allowed = set(year_filter)
            txns = [t for t in txns if t.booking_date.year in allowed]
        existing = _load_existing(
            bank,
            base_dir,
            year_filter,
            config.transactions_dir,
            metadata_date_keys=tuple(config.matching.metadata_date_keys),
            synthesize_from_metadata=dict(config.matching.synthesize_from_metadata),
        )
        work.append(_BankWork(bank, parser, txns, existing))

    transforms = load_transforms(config.transforms.enabled)
    working_rules: list[CategorizationRule] = list(session.rules)
    working_tag: ActiveTag | None = session.tag_state.active

    total = sum(len(bw.transactions) for bw in work)
    progress = 0
    results: list[ImportResult] = []
    quit_pending = False

    for bw in work:
        if quit_pending:
            break
        for txn in bw.transactions:
            progress += 1
            reporter.on_progress(progress, total, bw.bank.key)

            result, working_tag, working_rules = _process_transaction(
                txn=txn,
                bank=bw.bank,
                existing=bw.existing_entries,
                config=config,
                working_rules=working_rules,
                working_tag=working_tag,
                transforms_hooks=transforms,
                categorize_fn=categorize_fn,
                decisions=decisions,
                auto_threshold=session.options.auto_threshold,
            )

            decisions.record(txn, result)
            reporter.on_result(result)
            results.append(result)

            if result.action == "quit":
                quit_pending = True
                break

    return results


# ── Per-transaction processing ────────────────────────────────────────────────


def _process_transaction(
    *,
    txn: SourceTransaction,
    bank: BankConfig,
    existing: list[LedgerEntry],
    config,  # Config; type omitted to avoid a circular import at type level
    working_rules: list[CategorizationRule],
    working_tag: ActiveTag | None,
    transforms_hooks,
    categorize_fn: CategorizeFn,
    decisions: DecisionLog,
    auto_threshold: float | None,
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
            ImportResult(source_txn=txn, action="skip", proposal=None),
            _advance_tag(working_tag, txn.booking_date),
            working_rules,
        )

    # 3. Hard suppression — skip_update_patterns drop the proposal entirely.
    if _matches_skip_pattern(config.skip_update_patterns, txn):
        return (
            ImportResult(source_txn=txn, action="skip", proposal=None),
            _advance_tag(working_tag, txn.booking_date),
            working_rules,
        )

    # 4. Find rule + ranked candidates.
    rule = find_matching_rule(txn, working_rules)
    candidates = find_candidates(
        txn, existing, min_score=config.matching.min_score
    )

    # 5. Auto-categorize when score >= threshold and a rule is available.
    proposal: CategoryProposal
    if (
        auto_threshold is not None
        and candidates
        and candidates[0][1] >= auto_threshold
        and rule is not None
    ):
        proposal = _proposal_from_rule(rule)
    else:
        context = CategorizeContext(
            txn=txn,
            rules=tuple(working_rules),
            candidates=tuple(candidates),
            matched_rule=rule,
            active_tag=working_tag,
        )
        proposal = categorize_fn(context)

    # 6. Apply transforms only when there's a rule to drive them.
    if rule is not None:
        proposal = apply_transforms(transforms_hooks, proposal, txn, rule)

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

    # 9. Compute tag-state delta for "once" / expired "duration".
    next_tag = _advance_tag(working_tag, txn.booking_date)
    tag_delta: TagStateDelta | None = None
    if next_tag != working_tag and working_tag is not None:
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
    return result, next_tag, new_rules_list


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

"""Preview-only: bean-side reverse-provenance stats.

Exists for the `--preview` CLI command: per-section, per-year counts of
existing ledger entries and how many of them have a matching CSV row.
The match runs *backwards* — for each entry, does any CSV row hit on
amount + date? — so this module never goes through `pipeline.run`.

Subprocess-driven: when configured, `bean-query` is invoked to fetch the
plugin-expanded transaction count per (account, year). Failures are
swallowed; the preview line stays informational.
"""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from beancount_importer.config import BankConfig
from beancount_importer.models import LedgerEntry, SourceTransaction
from beancount_importer.pipeline._shared import (
    _load_all_outputs,
    _parse_all_inputs,
    _select_banks,
)
from beancount_importer.session import ImportSession


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

    # When the user passes `--bank`, `banks` is already filtered to the
    # one they asked about. Bean-side stats must follow suit — otherwise
    # the preview ends up with an extra "N26" / "TR" section the user
    # explicitly opted out of, plus a phantom file-bucketed copy of the
    # target bank shadowing its real section. The cleanest signal is
    # "did the user narrow the bank set?", which is "did `_select_banks`
    # drop any?".
    bank_scope_active = len(banks) < len(config.banks)

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
        elif bank_scope_active:
            # Out-of-scope entries are dropped entirely under `--bank`;
            # the user asked for one bank, the report should match.
            continue
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

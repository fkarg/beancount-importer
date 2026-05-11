"""Parse + load helpers shared by `run` and `preview`.

The two entry points (`run.run()` and `preview.compute_bean_provenance_stats()`)
both need to walk the configured CSVs and the existing ledger universe. Those
walks are pure functions of the session config and base directory — they live
here so neither caller has to depend on the other.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from beancount_importer.beancount_io.reader import read_ledger_multi
from beancount_importer.config import BankConfig
from beancount_importer.models import LedgerEntry, SourceTransaction
from beancount_importer.parsers.base import Parser
from beancount_importer.parsers.generic import GenericCsvParser
from beancount_importer.session import ImportSession


class _ParseErrorSink(Protocol):
    """Minimal sink for parse-error messages.

    Both `run.Reporter` and any preview-friendly reporter satisfy this
    structurally — kept narrow so this module doesn't need to import the
    full `Reporter` protocol.
    """

    def on_error(self, message: str) -> None: ...


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
    reporter: _ParseErrorSink | None,
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
            # Zero-amount rows produce no useful proposal and would
            # waste a categorizer prompt — drop before they enter the pipeline.
            rows = [t for t in rows if t.amount != 0]
            flat.extend(rows)
    return flat

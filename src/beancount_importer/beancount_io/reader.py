from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from beancount.loader import load_file
from beancount.core import data as bc_data

from beancount_importer.models import LedgerEntry


def read_ledger(path: Path, source_account: str) -> list[LedgerEntry]:
    """Load all Transaction entries from a .bean file for the given source account."""
    if not path.exists():
        return []

    entries, errors, _options = load_file(str(path))
    results: list[LedgerEntry] = []

    for entry in entries:
        if not isinstance(entry, bc_data.Transaction):
            continue
        entry_result = _extract_entry(entry, source_account, str(path))
        if entry_result is not None:
            results.append(entry_result)

    return results


def _extract_entry(
    txn: bc_data.Transaction,
    source_account: str,
    file_path: str,
) -> LedgerEntry | None:
    """Convert a beancount Transaction to LedgerEntry, keyed by source_account."""
    source_posting = None
    target_posting = None

    for posting in txn.postings:
        if posting.account == source_account:
            source_posting = posting
        else:
            if target_posting is None:
                target_posting = posting

    if source_posting is None or source_posting.units is None:
        return None

    amount = Decimal(str(source_posting.units.number))
    currency = source_posting.units.currency
    target_account = target_posting.account if target_posting else ""

    meta = dict(txn.meta)
    line_start = meta.pop("lineno", 0)

    # Collect posting-level metadata (e.g. sepa_ref, actual, paypal)
    for posting in txn.postings:
        if posting.meta:
            for k, v in posting.meta.items():
                if k not in ("filename", "lineno") and k not in meta:
                    meta[k] = str(v)

    meta.pop("filename", None)
    meta.pop("lineno", None)

    return LedgerEntry(
        date=txn.date,
        flag=txn.flag,
        payee=txn.payee,
        narration=txn.narration,
        source_account=source_account,
        target_account=target_account,
        amount=amount,
        currency=currency,
        metadata={k: str(v) for k, v in meta.items()},
        line_start=line_start,
        file_path=file_path,
    )

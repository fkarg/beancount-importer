from __future__ import annotations

import hashlib

from beancount_importer.models import SourceTransaction, LedgerEntry


def dedup_key(txn: SourceTransaction) -> str:
    """Primary dedup key: SEPA reference if present, else content hash."""
    if txn.sepa_reference:
        return f"sepa:{txn.sepa_reference}"
    return _content_hash(txn)


def _content_hash(txn: SourceTransaction) -> str:
    parts = "|".join([
        str(txn.booking_date),
        str(txn.amount),
        txn.currency,
        txn.bank_key,
        txn.payee or "",
        txn.description or "",
    ])
    digest = hashlib.sha256(parts.encode()).hexdigest()[:16]
    return f"hash:{digest}"


def is_duplicate(txn: SourceTransaction, existing: list[LedgerEntry]) -> bool:
    """Return True if any existing entry has the same dedup key."""
    key = dedup_key(txn)
    return any(_entry_key(entry) == key for entry in existing)


def _entry_key(entry: LedgerEntry) -> str:
    sepa = entry.metadata.get("sepa_ref") or entry.metadata.get("sepa_reference", "")
    if sepa:
        return f"sepa:{sepa}"
    parts = "|".join([
        str(entry.date),
        str(entry.amount),
        entry.currency,
        entry.source_account,
        entry.payee or "",
        entry.narration,
    ])
    digest = hashlib.sha256(parts.encode()).hexdigest()[:16]
    return f"hash:{digest}"

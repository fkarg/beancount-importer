"""Dedup: silent-skip on second import of the same row.

Callers must pre-filter `existing` to entries from the same bank as
`txn` — `is_duplicate` does *not* re-check the source account. This
matters because the txn-side carries `bank_key` (e.g. "spk") while
the entry-side carries `source_account` (e.g. "Assets:B:SPK"); they
intentionally don't appear in the hash so the two sides are
comparable. The pipeline's per-bank bucketing in
`_process_transaction`'s caller is what enforces the bank match.

Description-vs-narration: we hash on raw description on the txn side
and narration on the entry side. They line up *iff* the user hasn't
edited the bean file's narration after the first import. Edits break
dedup — that's a real risk for users who clean up their ledgers, but
the cure (storing the original raw description as metadata on every
entry) costs more than it pays. Cash withdrawals and other
SEPA-less rows fall back to this hash; everything with a SEPA ref
sails through the fast path.
"""

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
        txn.payee or "",
        txn.description or "",
    ])
    digest = hashlib.sha256(parts.encode()).hexdigest()[:16]
    return f"hash:{digest}"


def is_duplicate(txn: SourceTransaction, existing: list[LedgerEntry]) -> bool:
    """Return True if any existing entry has the same dedup key.

    Callers must have already filtered `existing` to the same-bank
    bucket; the hashes deliberately omit account/bank fields.
    """
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
        entry.payee or "",
        entry.narration,
    ])
    digest = hashlib.sha256(parts.encode()).hexdigest()[:16]
    return f"hash:{digest}"

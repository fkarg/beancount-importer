"""Dedup: silent-skip on second import of the same row.

Callers must pre-filter `existing` to entries from the same bank as
`txn` — `is_duplicate` does *not* re-check the source account. This
matters because the txn-side carries `bank_key` (e.g. "spk") while
the entry-side carries `source_account` (e.g. "Assets:B:SPK"); they
intentionally don't appear in the hash so the two sides are
comparable. The pipeline's per-bank bucketing in
`_process_transaction`'s caller is what enforces the bank match.

Two-key match: each side produces a SET of candidate keys (always a
content hash, plus a `sepa:<ref>` key when one is available), and a
duplicate is declared when the sets intersect. This handles the
common case where the CSV row carries a SEPA reference but the
existing bean entry — written by an importer that doesn't emit
`sepa_ref` metadata — only has the content-hash key. Without the
fallback, every SEPA-bearing row whose existing entry lacks the
metadata gets re-prompted on every import.

Description-vs-narration: the content hash uses raw description on the
txn side and narration on the entry side. They line up *iff* the user
hasn't edited the bean file's narration after the first import. Edits
break the content-hash path — that's a real risk for users who clean
up their ledgers, but the cure (storing the original raw description
on every entry) costs more than it pays. The SEPA path survives
narration edits as long as `sepa_ref` is present somewhere.
"""

from __future__ import annotations

import hashlib

from beancount_importer.models import SourceTransaction, LedgerEntry


def dedup_key(txn: SourceTransaction) -> str:
    """Primary dedup key: SEPA reference if present, else content hash.

    Kept for callers that want a single canonical key (e.g. logging or
    tests). Dedup itself uses `_txn_keys` / `_entry_keys`, which
    return *all* candidate keys so a SEPA-bearing txn still matches
    an entry that only carries the content-hash key.
    """
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


def _txn_keys(txn: SourceTransaction) -> set[str]:
    keys = {_content_hash(txn)}
    if txn.sepa_reference:
        keys.add(f"sepa:{txn.sepa_reference}")
    return keys


def _entry_keys(entry: LedgerEntry) -> set[str]:
    parts = "|".join([
        str(entry.date),
        str(entry.amount),
        entry.currency,
        entry.payee or "",
        entry.narration,
    ])
    digest = hashlib.sha256(parts.encode()).hexdigest()[:16]
    keys = {f"hash:{digest}"}
    sepa = entry.metadata.get("sepa_ref") or entry.metadata.get("sepa_reference", "")
    if sepa:
        keys.add(f"sepa:{sepa}")
    return keys


def is_duplicate(txn: SourceTransaction, existing: list[LedgerEntry]) -> bool:
    """Return True if any existing entry has the same dedup key.

    Callers must have already filtered `existing` to the same-bank
    bucket; the hashes deliberately omit account/bank fields.
    """
    return find_duplicate(txn, existing) is not None


def find_duplicate(
    txn: SourceTransaction, existing: list[LedgerEntry]
) -> LedgerEntry | None:
    """Return the first entry whose keys overlap `txn`'s, or None.

    Used by the pipeline to "claim" the matched entry — without it,
    two identical CSV rows would both dedup-skip pointing at the
    *same* entry, leaving its sibling looking like a CSV-orphan in
    the preview report.
    """
    keys = _txn_keys(txn)
    for entry in existing:
        if keys & _entry_keys(entry):
            return entry
    return None

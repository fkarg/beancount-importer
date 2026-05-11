"""Dedup: silent-skip on second import of the same row.

Callers must pre-filter `existing` to entries from the same bank as
`txn` — `find_definitive_duplicate` does *not* re-check the source
account. The pipeline's per-bank bucketing enforces the bank match.

Strategy: SEPA-reference equality wins definitively. Otherwise filter
candidates by `(amount, currency)` and require at least one txn-side
date (booking or value date) to land within `max_date_days` of any
entry-side date (entry.date or one of its metadata_dates, i.e. the
plugin-expanded `actual:` / `paypal:` / `settle:` dates). The function
returns a single matching candidate only — when two or more entries
fit, dedup deliberately abstains so the scorer + merge prompt can
disambiguate (handles the common case of multiple identical
amount/date pairs in the same week).

`value_date` is checked first on the txn side because plugin-rewritten
entries usually land on the value-date, biasing the cheap path toward
the hit. SEPA shortcuts all of this.
"""

from __future__ import annotations

from datetime import date

from beancount_importer.models import LedgerEntry, SourceTransaction


def _txn_candidate_dates(txn: SourceTransaction) -> tuple[date, ...]:
    """Dates the txn side might collide on. `value_date` first because it
    matches plugin-rewritten entries (`actual:`/`paypal:`/`settle:`) more
    often than the raw booking date."""
    if txn.value_date is None:
        return (txn.booking_date,)
    if txn.value_date == txn.booking_date:
        return (txn.booking_date,)
    return (txn.value_date, txn.booking_date)


def _entry_candidate_dates(entry: LedgerEntry) -> tuple[date, ...]:
    """Entry-side dates: the entry's own date plus any metadata-derived
    dates from `actual:`/`paypal:`/`settle:` postings."""
    return (entry.date, *entry.metadata_dates)


def _entry_sepa(entry: LedgerEntry) -> str:
    return entry.metadata.get("sepa_ref") or entry.metadata.get("sepa_reference", "")


def find_definitive_duplicate(
    txn: SourceTransaction,
    entries: list[LedgerEntry],
    *,
    max_date_days: int,
) -> LedgerEntry | None:
    """Return the sole entry that definitively duplicates `txn`, or None.

    Match rules:
    - If `txn.sepa_reference` is non-empty, the first entry with the same
      `sepa_ref`/`sepa_reference` metadata wins immediately (date and amount
      ignored — the reference is the user's source of truth).
    - Otherwise: filter `entries` by exact (amount, currency) match, then
      require any txn-side candidate date to be within `max_date_days` of
      any entry-side candidate date. If exactly one entry passes, return it.
      If zero or 2+ entries pass, return None so the scorer + merge prompt
      can decide.

    Returning None on multi-candidate cases is deliberate: two identical
    same-week rows are exactly the situation where a silent dedup-skip
    would attach the wrong CSV row to the wrong entry. The merge prompt
    sees both and gives the user the choice.
    """
    if txn.sepa_reference:
        for entry in entries:
            if _entry_sepa(entry) == txn.sepa_reference:
                return entry
        # No SEPA-side match: fall through to date/amount path. Some banks
        # ship SEPA-bearing rows whose existing entry was written by a
        # different importer that never persisted the reference; the date
        # window is enough to recognise them.

    txn_dates = _txn_candidate_dates(txn)
    matches: list[LedgerEntry] = []
    for entry in entries:
        # Inferred-amount entries are cross-bank transit legs — let them
        # flow through to the scorer + `_diff_changes`, which can propose
        # `actual:`/`settle:`/`paypal:` metadata when the two CSV dates
        # disagree. Dedup-silent-skipping them here would deny that
        # affordance.
        if entry.amount_inferred:
            continue
        if entry.amount != txn.amount:
            continue
        if entry.currency != txn.currency:
            continue
        entry_dates = _entry_candidate_dates(entry)
        if any(
            abs((td - ed).days) <= max_date_days
            for td in txn_dates
            for ed in entry_dates
        ):
            matches.append(entry)
            if len(matches) > 1:
                return None
    return matches[0] if matches else None


def is_duplicate(
    txn: SourceTransaction,
    existing: list[LedgerEntry],
    *,
    max_date_days: int = 5,
) -> bool:
    """Thin wrapper for callers that only need a boolean."""
    return (
        find_definitive_duplicate(txn, existing, max_date_days=max_date_days)
        is not None
    )

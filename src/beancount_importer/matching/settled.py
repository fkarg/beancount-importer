"""Skip CSV rows that are already booked via intermediary-settlement metadata.

When an existing ledger entry carries a metadata-date key (`paypal:`,
`settle:`, `actual:`, or anything else in `MatchingConfig.metadata_date_keys`),
the user has already accounted for both legs of an intermediary-settled
transaction — typically the merchant entry on the bank side annotated with
`paypal: <date>`, where a beancount plugin (`settle_inv`) splits it at load
time into a bank-side debit + PayPal-side credit. Importing the corresponding
row from the intermediary's own CSV on the next run would re-book the same
flow.

This matcher silent-skips a CSV row when an existing entry has *any*
metadata_dates set AND the row's `(currency, |amount|, date)` matches the
entry on either side of the intermediary:

- the entry's booking date (the settle side), or
- any of the entry's `metadata_dates` (the actual / intermediary-recorded side).

Amount is compared by absolute value so flows that swap direction across
banks (SPK outflow ↔ PayPal "Bank Deposit" inflow) still pair up.

The check uses `LedgerEntry.metadata_dates`, which the reader populates from
`MatchingConfig.metadata_date_keys` — so the set of keys treated as
"settlement evidence" is configurable without the matcher knowing about
config. Users with a different plugin convention (e.g. `cleared:` instead
of `settle:`) just add their key to that list.
"""

from __future__ import annotations

from beancount_importer.matching.registry import MatchOutcome
from beancount_importer.models import LedgerEntry, SourceTransaction


class _IntermediarySettlementMatcher:
    name = "intermediary_settlement"

    def match(
        self,
        txn: SourceTransaction,
        all_csv_by_bank: dict[str, list[SourceTransaction]],
        existing_entries: list[LedgerEntry],
    ) -> MatchOutcome | None:
        del all_csv_by_bank
        target_amount = abs(txn.amount)
        target_date = txn.booking_date
        for entry in existing_entries:
            if not entry.metadata_dates:
                continue
            if entry.currency != txn.currency:
                continue
            if abs(entry.amount) != target_amount:
                continue
            if entry.date == target_date or target_date in entry.metadata_dates:
                return MatchOutcome(
                    kind="skip",
                    reason="settled_via_intermediary",
                    matched_entry=entry,
                )
        return None


hook = _IntermediarySettlementMatcher()

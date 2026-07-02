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
metadata_dates set AND the row's `(currency, |amount|, date, text)` matches
the entry on either side of the intermediary:

- the entry's booking date (the settle side), or
- any of the entry's `metadata_dates` (the actual / intermediary-recorded side).

Amount is compared by absolute value so flows that swap direction across
banks (SPK outflow ↔ PayPal "Bank Deposit" inflow) still pair up.

A token-set similarity floor (`TEXT_FLOOR`) is required between the row's
payee+description and the entry's payee+narration — without it, any unrelated
entry sharing the same currency, |amount| and (entry-date OR metadata-date)
would silently swallow the CSV row. The floor is permissive (30/100) because
intermediary CSVs frequently use the funding bank's terse payee while the
ledger entry carries the merchant name, so the overlap can be just one
shared token.

Exception: a *merchant-less* intermediary top-up — PayPal's "Bank Deposit to
PP Account" carries a generic description and NO payee — has no merchant token
to match against the settled entry, so the floor would always reject it. For
those rows the floor is waived and `|amount| + (date or metadata-date)` against
a metadata-bearing, text-carrying entry is enough (matching the reference
importer). Rows *with* a payee still require the floor.

The check uses `LedgerEntry.metadata_dates`, which the reader populates from
`MatchingConfig.metadata_date_keys` — so the set of keys treated as
"settlement evidence" is configurable without the matcher knowing about
config. Users with a different plugin convention (e.g. `cleared:` instead
of `settle:`) just add their key to that list.
"""

from __future__ import annotations

from beancount_importer.matching.registry import MatchOutcome
from beancount_importer.matching.scorer import similarity_score
from beancount_importer.models import LedgerEntry, SourceTransaction

# Token-set ratio (0-100) on normalized payee+description vs payee+narration.
# Permissive: legitimate intermediary matches often share only the merchant
# token (e.g. "Google Payment" ↔ "Google"). Anything below this threshold
# is almost certainly an amount/date coincidence on an unrelated entry.
TEXT_FLOOR = 30.0


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
        txn_text = " ".join(filter(None, [txn.payee, txn.description]))
        if not txn_text:
            # Without identifying text on the CSV side we can't tell a real
            # settlement match from an amount/date coincidence — leave the
            # row for normal dedup/scoring instead of silent-skipping.
            return None
        # Merchant-less intermediary top-ups have no payee (only a generic
        # description); the similarity floor is waived for them below.
        has_payee = bool(txn.payee and txn.payee.strip())
        for entry in existing_entries:
            if not entry.metadata_dates:
                continue
            if entry.currency != txn.currency:
                continue
            if abs(entry.amount) != target_amount:
                continue
            if entry.date != target_date and target_date not in entry.metadata_dates:
                continue
            entry_text = " ".join(filter(None, [entry.payee, entry.narration]))
            if not entry_text:
                continue
            # A payee-carrying row must clear the floor; a merchant-less
            # top-up (no payee) relies on |amount| + date alone.
            if has_payee and similarity_score(txn_text, entry_text) < TEXT_FLOOR:
                continue
            return MatchOutcome(
                kind="skip",
                reason="settled_via_intermediary",
                matched_entry=entry,
            )
        return None


hook = _IntermediarySettlementMatcher()

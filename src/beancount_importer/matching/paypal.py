"""PayPal cross-referencing.

A PayPal-funded purchase shows up twice in source data:
- once on the funding bank's CSV (e.g. SPK), with payee="PayPal"
- once on the PayPal CSV, with payee=actual merchant

We want the first to become an internal transfer (Bank → PayPal) and only the
PayPal-side row to record the merchant. `find_paypal_counterpart` does the
amount + date match; the pipeline rewrites the bank-side proposal accordingly.
"""

from __future__ import annotations

from collections.abc import Iterable

from beancount_importer.matching.registry import MatchOutcome
from beancount_importer.models import LedgerEntry, SourceTransaction


# The PayPal account is the user-side leg the bank-funding row should book to.
# It's hard-coded for the common SPK/N26-funded-PayPal case; if a user runs
# multiple PayPal accounts they can copy this matcher and tweak the target.
_PAYPAL_ACCOUNT = "Assets:B:PayPal"


def find_paypal_counterpart(
    bank_txn: SourceTransaction,
    paypal_txns: Iterable[SourceTransaction],
    *,
    tolerance_days: int = 7,
) -> SourceTransaction | None:
    """Return the PayPal-side transaction that funds `bank_txn`, if any.

    The bank side debits the user's bank by the same amount that PayPal then
    spends on a merchant. Sign and amount must match exactly (PayPal pulls the
    full transfer in one go); date can drift by a few days because the bank
    posts when the SEPA settles, while PayPal records the user-action time.
    """
    candidates: list[tuple[int, SourceTransaction]] = []
    for pp in paypal_txns:
        if pp.amount != bank_txn.amount:
            continue
        if pp.currency != bank_txn.currency:
            continue
        days = abs((pp.booking_date - bank_txn.booking_date).days)
        if days > tolerance_days:
            continue
        candidates.append((days, pp))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def is_paypal_funding_txn(txn: SourceTransaction) -> bool:
    """Heuristic: does this bank-side row look like it was funded via PayPal?

    Used as a cheap pre-filter before invoking `find_paypal_counterpart`, so
    we don't iterate the full PayPal CSV for every unrelated transaction.
    """
    text = f"{(txn.payee or '').lower()} {(txn.description or '').lower()}"
    return "paypal" in text


class _PayPalCounterpartMatcher:
    """When a bank-side row is funded by PayPal and the PayPal CSV records
    the matching counterpart, rewrite the proposal to be a transfer to the
    user's PayPal account. The PayPal-CSV row separately books the merchant.
    """

    name = "paypal_counterpart"

    def match(
        self,
        txn: SourceTransaction,
        all_csv_by_bank: dict[str, list[SourceTransaction]],
        existing_entries: list[LedgerEntry],
    ) -> MatchOutcome | None:
        del existing_entries
        # Only candidate rows on a non-PayPal bank can fund a PayPal balance.
        if txn.bank_key == "paypal" or not is_paypal_funding_txn(txn):
            return None
        paypal_txns = all_csv_by_bank.get("paypal", [])
        cp = find_paypal_counterpart(txn, paypal_txns)
        if cp is None:
            return None
        return MatchOutcome(
            kind="rewrite_target",
            reason="paypal_counterpart",
            target_account=_PAYPAL_ACCOUNT,
            metadata={"paypal": cp.booking_date.isoformat()},
            matched_txn=cp,
        )


hook = _PayPalCounterpartMatcher()

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

from beancount_importer.models import SourceTransaction


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

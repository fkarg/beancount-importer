"""Internal-transfer detection.

Two complementary mechanisms:

1. `is_likely_internal_transfer(txn)` — pure heuristic over a single
   `SourceTransaction`. Looks at description/payee for transfer keywords
   ("Überweisung", "Transfer", "Depot", PayPal, …) and round-amount tells.
   Used to *propose* a transfer target during interactive categorization.

2. `find_existing_counterparty(txn, entries, ...)` — given a transaction and
   the union of `LedgerEntry`s across all banks, look for one that already
   represents the *other side* of this transfer (reversed sign, same currency,
   close date). Used to *suppress* a duplicate when a transfer has already been
   booked from the counterparty's bank file.
"""

from __future__ import annotations

from collections.abc import Iterable

from beancount_importer.matching.normalize import normalize_text
from beancount_importer.matching.registry import MatchOutcome
from beancount_importer.models import LedgerEntry, SourceTransaction


# Keywords that strongly suggest this transaction is a transfer between
# the user's own accounts rather than a regular expense or income.
_TRANSFER_KEYWORDS = (
    "uberweisung",  # normalized: "Überweisung" → "uberweisung"
    "transfer",
    "depot",
    "aufladung",
    "withdraw",
    "deposit",
    "gutschrift",
)

# Substrings that map to a guessed counterparty account. Order matters:
# the first match wins, so put the more specific keys first.
_BANK_TARGETS: tuple[tuple[str, str], ...] = (
    ("trade republic", "Assets:B:TR:Cash"),
    ("traderepublic", "Assets:B:TR:Cash"),
    ("paypal", "Assets:B:PayPal"),
    ("n26", "Assets:B:N26"),
    ("sparkasse", "Assets:B:SPK"),
)

# URL/merchant tells that override the round-amount heuristic — a "round" PayPal
# charge with a merchant URL in the description is almost always a purchase,
# not a top-up.
_PURCHASE_INDICATORS = ("www.", "http", ".com", ".de", ".net", ".org")


def is_likely_internal_transfer(txn: SourceTransaction) -> tuple[bool, str | None]:
    """Heuristic: would this look like an own-bank-to-own-bank transfer?

    Returns `(is_transfer, suggested_target_account)`. The target may be `None`
    even when `is_transfer` is True — the caller should fall back to a generic
    transit account in that case.
    """
    desc = normalize_text(txn.description or "")
    payee = normalize_text(txn.payee or "")
    text = f"{payee} {desc}"

    has_keyword = any(kw in text for kw in _TRANSFER_KEYWORDS)

    cents = abs(int(txn.amount * 100))
    is_round = cents > 0 and cents % 5000 == 0

    target: str | None = None
    has_bank_name = False
    for needle, account in _BANK_TARGETS:
        if needle in text:
            target = account
            has_bank_name = True
            break

    has_purchase = any(ind in text for ind in _PURCHASE_INDICATORS)

    is_transfer = has_keyword or (has_bank_name and is_round and not has_purchase)

    # PayPal is special: from any bank's perspective, money sent to PayPal is
    # always a transfer to the user's PayPal balance — even if a purchase URL
    # is in the description, the bank doesn't pay the merchant directly.
    if target == "Assets:B:PayPal":
        is_transfer = True

    return is_transfer, target


def find_existing_counterparty(
    txn: SourceTransaction,
    entries: Iterable[LedgerEntry],
    *,
    tolerance_days: int = 5,
    internal_account_prefixes: tuple[str, ...] = ("Assets:B:", "Liabilities:CreditCard:"),
) -> LedgerEntry | None:
    """Find an already-booked counterparty for `txn`, if any.

    A counterparty entry must:
    - sit on an internal-transfer account (other than `txn.bank_key`'s own),
    - have the *opposite* sign and same absolute amount,
    - be within `tolerance_days` of the transaction's booking date,
    - share the currency.

    Returns the closest match by date, or `None`.
    """
    candidates: list[tuple[int, LedgerEntry]] = []
    target_amount = -txn.amount
    for entry in entries:
        if entry.amount != target_amount:
            continue
        if entry.currency != txn.currency:
            continue
        if not any(entry.source_account.startswith(p) for p in internal_account_prefixes):
            continue
        days = abs((entry.date - txn.booking_date).days)
        if days > tolerance_days:
            continue
        candidates.append((days, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


class _ExistingTransferMatcher:
    """When a CSV row looks like an internal transfer and the counterparty
    leg is already booked in the ledger, skip the row — re-importing would
    create a second booking of the same money movement.
    """

    name = "existing_transfer"

    def match(
        self,
        txn: SourceTransaction,
        all_csv_by_bank: dict[str, list[SourceTransaction]],
        existing_entries: list[LedgerEntry],
    ) -> MatchOutcome | None:
        del all_csv_by_bank
        is_transfer, _ = is_likely_internal_transfer(txn)
        if not is_transfer:
            return None
        cp = find_existing_counterparty(txn, existing_entries)
        if cp is None:
            return None
        return MatchOutcome(
            kind="skip",
            reason="counterpart_already_booked",
            matched_entry=cp,
        )


hook = _ExistingTransferMatcher()

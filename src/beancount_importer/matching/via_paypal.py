"""Link PayPal CSV rows to `via_paypal: TRUE` placeholder entries.

A PayPal-funded bank row categorized straight to an expense is written with
a `via_paypal: TRUE` marker on the bank posting when the PayPal CSV wasn't
available to pin the PayPal-side date (see `_result._via_paypal_marker`).
When the PayPal row arrives in a later run, this matcher pairs it with the
placeholder and emits `link_placeholder`; the pipeline rewrites the entry in
place, upgrading the marker to posting-level `paypal: <PayPal date>` — the
shape the `settle_inv` plugin splits at load time and that re-imports
recognise as settled. The CSV row is consumed; nothing is written to the
PayPal file.

Match conditions: entry carries the marker, is a plain two-posting entry,
same currency, *exact signed* amount (a deposit row must never link a
purchase placeholder), booking dates within `MAX_DATE_DAYS`, and a
permissive text floor — same rationale as `settled.TEXT_FLOOR`: without it
any coincidental amount+date pair would silently rewrite an unrelated
placeholder. Ambiguity aborts: two placeholders fitting one row fall
through to normal categorization rather than guessing (the old importer
picked the first match; abstaining is safer).
"""

from __future__ import annotations

from beancount_importer.matching.registry import MatchOutcome
from beancount_importer.matching.scorer import similarity_score
from beancount_importer.models import LedgerEntry, SourceTransaction

# Same permissive floor as the settled matcher: the placeholder often
# carries a terse payee ("PayPal") while the PayPal CSV names the merchant.
TEXT_FLOOR = 30.0

# Bank booking lags the PayPal-side purchase by a few days at most; the old
# importer used the same window.
MAX_DATE_DAYS = 7


class _ViaPaypalPlaceholderMatcher:
    name = "via_paypal_placeholder"

    def match(
        self,
        txn: SourceTransaction,
        all_csv_by_bank: dict[str, list[SourceTransaction]],
        existing_entries: list[LedgerEntry],
    ) -> MatchOutcome | None:
        del all_csv_by_bank
        txn_text = " ".join(filter(None, [txn.payee, txn.description]))
        if not txn_text:
            # No identifying text — can't tell a real link from an
            # amount/date coincidence.
            return None
        candidates: list[LedgerEntry] = []
        for entry in existing_entries:
            if entry.metadata.get("via_paypal", "").upper() != "TRUE":
                continue
            if entry.has_multiple_postings:
                continue
            if entry.currency != txn.currency or entry.amount != txn.amount:
                continue
            if abs((entry.date - txn.booking_date).days) > MAX_DATE_DAYS:
                continue
            entry_text = " ".join(filter(None, [entry.payee, entry.narration]))
            if similarity_score(txn_text, entry_text) < TEXT_FLOOR:
                continue
            candidates.append(entry)
        if len(candidates) != 1:
            return None
        return MatchOutcome(
            kind="link_placeholder",
            reason="via_paypal_placeholder",
            matched_entry=candidates[0],
        )


hook = _ViaPaypalPlaceholderMatcher()

"""PayPal CSV parser.

PayPal exports use a wider, multilingual schema than typical bank CSVs:
- Column names appear in English (`Date`, `Net`, `Currency`) and German (`Datum`,
  `Netto`, `Währung`); we accept either to avoid users having to re-export.
- A `Balance Impact` of `Memo` marks informational rows (auth holds, reversals)
  that don't move money — those are skipped.
- The `Net` amount is the post-fee number we want booked. `Gross` is a fallback
  for older exports.
- Timestamps include a time component which we keep in metadata so transfer
  matching can use it as a tiebreaker.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from collections.abc import Iterator

from beancount_importer.config import BankConfig
from beancount_importer.models import SourceTransaction
from beancount_importer.parsers.locale import parse_amount, parse_date


_DATE_KEYS = ("Date", "Datum")
_TIME_KEYS = ("Time", "Zeit")
_NET_KEYS = ("Net", "Netto", "Gross", "Brutto")
_CURRENCY_KEYS = ("Currency", "Währung", "Waehrung")
_NAME_KEYS = ("Name", "Absender E-Mail-Adresse", "From Email Address")
_DESC_KEYS = ("Description", "Subject", "Betreff", "Item Title")
_REF_KEYS = ("Transaction ID", "Transaktionscode")
_REFTXN_KEYS = ("Reference Txn ID", "Zugehöriger Transaktionscode")
_IMPACT_KEYS = ("Balance Impact",)

# PayPal splits a foreign-currency purchase into a bundle linked by
# `Reference Txn ID`: the payment (foreign currency) plus two "General
# Currency Conversion" legs — one in the payment's currency (the FX swap,
# nets to zero) and one in the home currency (the real balance change). We
# collapse the payment + its two GCC legs into a single `@@`-priced txn.
# English description only for now; German exports add their own label.
_GCC_DESC = "General Currency Conversion"

# An authorization hold and its reversal are an exact ±X mirror pair linked by
# `Reference Txn ID` (the reversal references the hold). They net to zero and
# the real charge posts as its own row, so both legs are pure noise we drop.
_HOLD_DESC = "Account Hold for Open Authorization"
_REVERSAL_DESC = "Reversal of General Account Hold"


def _first(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if v:
            return v.strip()
    return ""


class PayPalParser:
    """Parser for PayPal activity CSV exports."""

    def __init__(self, bank_config: BankConfig) -> None:
        self._cfg = bank_config
        self._csv = bank_config.csv

    @property
    def bank_key(self) -> str:
        return self._cfg.key

    @property
    def header_signature(self) -> frozenset[str]:
        return frozenset(_DATE_KEYS + _NET_KEYS + _NAME_KEYS)

    def parse(self, file_path: str) -> Iterator[SourceTransaction]:
        with open(file_path, encoding=self._csv.encoding, newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter=self._csv.delimiter))
        yield from self._emit(rows)

    def _emit(self, rows: list[dict[str, str]]) -> Iterator[SourceTransaction]:
        """Yield transactions after collapsing PayPal currency-conversion bundles.

        Buffering (rather than streaming) is required because a foreign
        purchase only becomes a single priced transaction once its two
        General Currency Conversion legs — which appear as separate rows —
        have been folded in.
        """
        by_id = {tid: r for r in rows if (tid := _first(r, _REF_KEYS))}
        suppress, collapse = self._plan_currency_conversions(rows, by_id)
        suppress |= self._plan_hold_reversals(rows, by_id)
        for row in rows:
            tid = _first(row, _REF_KEYS)
            if tid and tid in suppress:
                continue
            txn = self._parse_row(row)
            if txn is None:
                continue
            if tid in collapse:
                txn = txn.model_copy(update=collapse[tid])
            yield txn

    def _plan_currency_conversions(
        self, rows: list[dict[str, str]], by_id: dict[str, dict[str, str]]
    ) -> tuple[set[str], dict[str, dict[str, object]]]:
        """Return (GCC-leg ids to drop, payment-id → collapsed-field overrides).

        A bundle qualifies only when a payment has exactly one home-currency
        and one same-currency GCC leg; anything else is left untouched so no
        data is silently lost.
        """
        legs_by_parent: dict[str, list[dict[str, str]]] = {}
        for r in rows:
            if _first(r, _DESC_KEYS) == _GCC_DESC:
                legs_by_parent.setdefault(_first(r, _REFTXN_KEYS), []).append(r)

        def ccy(row: dict[str, str]) -> str:
            return _first(row, _CURRENCY_KEYS) or "EUR"

        suppress: set[str] = set()
        collapse: dict[str, dict[str, object]] = {}
        for parent_id, legs in legs_by_parent.items():
            parent = by_id.get(parent_id)
            if parent is None:
                # Empty ref, or a parent outside this file — can't collapse.
                continue
            home = [leg for leg in legs if ccy(leg) != ccy(parent)]
            foreign = [leg for leg in legs if ccy(leg) == ccy(parent)]
            if len(home) != 1 or len(foreign) != 1:
                continue
            home_net = parse_amount(_first(home[0], _NET_KEYS), self._csv.amount_locale)
            parent_net = parse_amount(_first(parent, _NET_KEYS), self._csv.amount_locale)
            collapse[parent_id] = {
                "amount": home_net,
                "currency": ccy(home[0]),
                "original_amount": abs(parent_net),
                "original_currency": ccy(parent),
            }
            suppress.update(_first(leg, _REF_KEYS) for leg in legs)
        return suppress, collapse

    def _plan_hold_reversals(
        self, rows: list[dict[str, str]], by_id: dict[str, dict[str, str]]
    ) -> set[str]:
        """Return ids of hold↔reversal mirror pairs to drop.

        Only an exact match is dropped: the reversal must reference an
        `Account Hold for Open Authorization` row whose `Net` is the exact
        negation of the reversal's. An unpaired hold (no matching reversal)
        is kept — a hold that became a real charge must not vanish.
        """
        suppress: set[str] = set()
        for r in rows:
            if _first(r, _DESC_KEYS) != _REVERSAL_DESC:
                continue
            hold = by_id.get(_first(r, _REFTXN_KEYS))
            if hold is None or _first(hold, _DESC_KEYS) != _HOLD_DESC:
                continue
            r_net = parse_amount(_first(r, _NET_KEYS), self._csv.amount_locale)
            h_net = parse_amount(_first(hold, _NET_KEYS), self._csv.amount_locale)
            if r_net != -h_net:
                continue
            suppress.update({_first(r, _REF_KEYS), _first(hold, _REF_KEYS)})
        return suppress

    def _parse_row(self, row: dict[str, str]) -> SourceTransaction | None:
        if _first(row, _IMPACT_KEYS) == "Memo":
            return None

        date_str = _first(row, _DATE_KEYS)
        if not date_str:
            return None
        booking_date = parse_date(date_str, self._csv.date_format)

        amount_str = _first(row, _NET_KEYS)
        if not amount_str:
            return None
        amount = parse_amount(amount_str, self._csv.amount_locale)

        currency = _first(row, _CURRENCY_KEYS) or "EUR"
        payee = _first(row, _NAME_KEYS) or None
        description = _first(row, _DESC_KEYS) or None
        ref = _first(row, _REF_KEYS)
        time_str = _first(row, _TIME_KEYS)

        raw = dict(row)
        if time_str:
            raw["_time"] = time_str

        original_amount: Decimal | None = None
        original_currency: str | None = None
        if self._csv.field_original_amount:
            oa_str = row.get(self._csv.field_original_amount, "").strip()
            if oa_str:
                original_amount = parse_amount(oa_str, self._csv.amount_locale)
        if self._csv.field_original_currency:
            original_currency = row.get(self._csv.field_original_currency, "").strip() or None

        return SourceTransaction(
            booking_date=booking_date,
            amount=amount,
            currency=currency,
            description=description,
            payee=payee,
            bank_key=self._cfg.key,
            sepa_reference=ref,
            raw_data=raw,
            original_amount=original_amount,
            original_currency=original_currency,
        )

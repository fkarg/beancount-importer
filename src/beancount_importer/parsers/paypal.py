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
_IMPACT_KEYS = ("Balance Impact",)


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
            reader = csv.DictReader(fh, delimiter=self._csv.delimiter)
            for row in reader:
                txn = self._parse_row(row)
                if txn is not None:
                    yield txn

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

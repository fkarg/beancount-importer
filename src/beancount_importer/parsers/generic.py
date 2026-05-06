from __future__ import annotations

import csv
from decimal import Decimal
from typing import Iterator

from beancount_importer.config import BankConfig
from beancount_importer.models import SourceTransaction
from beancount_importer.parsers.locale import parse_amount, parse_date


# Order: utf-8-sig handles BOMed exports cleanly; plain utf-8 next; then the
# Western-European single-byte fallbacks Sparkasse / older N26 actually ship.
_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1")


def _read_with_fallback(file_path: str, primary: str) -> str:
    candidates: list[str] = [primary]
    for enc in _FALLBACK_ENCODINGS:
        if enc not in candidates:
            candidates.append(enc)
    for enc in candidates:
        try:
            with open(file_path, encoding=enc, newline="") as fh:
                text = fh.read()
            # Strip a leading BOM regardless of which encoding succeeded.
            # Plain "utf-8" happily decodes a BOMed file (the 3 BOM bytes ARE
            # valid UTF-8 for U+FEFF), so the fallback never kicks in — but the
            # surviving ﻿ corrupts the first CSV column name. PayPal's
            # exports are the canonical case. Strip unconditionally; any
            # encoding that produced a leading ﻿ did so erroneously.
            return text.lstrip("﻿")
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        primary, b"", 0, 0,
        f"could not decode {file_path} with any of {candidates}",
    )


class GenericCsvParser:
    """Config-driven parser; handles any bank described entirely by BankConfig.csv."""

    def __init__(self, bank_config: BankConfig) -> None:
        self._cfg = bank_config
        self._csv = bank_config.csv

    @property
    def bank_key(self) -> str:
        return self._cfg.key

    @property
    def header_signature(self) -> frozenset[str]:
        cols = {self._csv.field_date, self._csv.field_amount}
        for name in (
            self._csv.field_value_date,
            self._csv.field_currency,
            self._csv.field_payee,
            self._csv.field_sepa_reference,
            self._csv.field_original_amount,
            self._csv.field_original_currency,
        ):
            if name:
                cols.add(name)
        if isinstance(self._csv.field_description, list):
            cols.update(self._csv.field_description)
        elif self._csv.field_description:
            cols.add(self._csv.field_description)
        return frozenset(cols)

    def parse(self, file_path: str) -> Iterator[SourceTransaction]:
        csv_cfg = self._csv
        # Banks ship CSVs in mixed encodings — Sparkasse exports are typically
        # cp1252/latin-1 (the BOM-prefixed UTF-8 variant only landed recently).
        # Try the configured encoding first, then walk through the usual German
        # bank fallbacks. We decode upfront so a mid-file invalid byte fails
        # fast instead of yielding partial rows.
        text = _read_with_fallback(file_path, csv_cfg.encoding)
        reader = csv.DictReader(text.splitlines(), delimiter=csv_cfg.delimiter)
        for row in reader:
            txn = self._parse_row(row)
            if txn is not None:
                yield txn

    def _parse_row(self, row: dict[str, str]) -> SourceTransaction | None:
        csv_cfg = self._csv

        # Skip rows based on skip_row_where
        for col, val in csv_cfg.skip_row_where.items():
            if row.get(col, "").strip() == val:
                return None

        raw_amount_str = row[csv_cfg.field_amount].strip()
        if not raw_amount_str:
            return None

        amount = parse_amount(raw_amount_str, csv_cfg.amount_locale)

        if csv_cfg.skip_zero_amounts and amount == Decimal(0):
            return None

        booking_date = parse_date(row[csv_cfg.field_date], csv_cfg.date_format)

        value_date = None
        if csv_cfg.field_value_date:
            vd_str = row.get(csv_cfg.field_value_date, "").strip()
            if vd_str:
                value_date = parse_date(vd_str, csv_cfg.date_format)

        currency = "EUR"
        if csv_cfg.field_currency:
            currency = row.get(csv_cfg.field_currency, "EUR").strip() or "EUR"

        payee = None
        if csv_cfg.field_payee:
            payee = row.get(csv_cfg.field_payee, "").strip() or None

        description: str | None = None
        desc_fields = (
            csv_cfg.field_description
            if isinstance(csv_cfg.field_description, list)
            else [csv_cfg.field_description]
        )
        parts = [row.get(f, "").strip() for f in desc_fields if f]
        joined = " ".join(p for p in parts if p)
        description = joined or None

        sepa_reference = ""
        if csv_cfg.field_sepa_reference:
            sepa_reference = row.get(csv_cfg.field_sepa_reference, "").strip()

        original_amount: Decimal | None = None
        if csv_cfg.field_original_amount:
            oa_str = row.get(csv_cfg.field_original_amount, "").strip()
            if oa_str:
                original_amount = parse_amount(oa_str, csv_cfg.amount_locale)

        original_currency: str | None = None
        if csv_cfg.field_original_currency:
            original_currency = row.get(csv_cfg.field_original_currency, "").strip() or None

        return SourceTransaction(
            booking_date=booking_date,
            value_date=value_date,
            amount=amount,
            currency=currency,
            description=description,
            payee=payee,
            bank_key=self._cfg.key,
            sepa_reference=sepa_reference,
            raw_data=dict(row),
            original_amount=original_amount,
            original_currency=original_currency,
        )

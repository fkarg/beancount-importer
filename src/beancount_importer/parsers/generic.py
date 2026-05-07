from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
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


def _cell_to_str(value: object) -> str:
    """Best-effort coerce an xlrd cell value to the string shape `_parse_row` expects.

    xlrd hands back floats for numeric cells and Python strings for text. We
    keep numbers as their plain decimal repr (no scientific notation) so the
    locale-aware amount/date parsers see the same string content they would
    from a CSV column.
    """
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10f}".rstrip("0").rstrip(".")
    if value is None:
        return ""
    return str(value)


def _read_xls_rows(
    file_path: str, field_date: str, field_amount: str
) -> Iterator[dict[str, str]]:
    """Read the first sheet of an .xls workbook and yield CSV-DictReader-shaped rows.

    Bank xls exports usually carry several rows of human-readable preamble
    (account holder, date range, balance) before the actual table — the Zinia
    "Amazon Visa" export ships 11 such rows. We auto-locate the header row by
    scanning for one that contains both `field_date` and `field_amount`, then
    treat every subsequent row whose date column is non-empty as a data row
    (skipping the blank separator row Zinia injects between header and body).

    `xlrd<2.0` is the only library that still reads the legacy .xls binary
    format; openpyxl handles xlsx only. We import it lazily so users who never
    touch xls don't pay the import cost.
    """
    import xlrd  # type: ignore[import-not-found]

    workbook = xlrd.open_workbook(file_path)
    sheet = workbook.sheet_by_index(0)

    header_row_idx = -1
    for r in range(sheet.nrows):
        cells = [_cell_to_str(c).strip() for c in sheet.row_values(r)]
        if field_date in cells and field_amount in cells:
            header_row_idx = r
            break
    if header_row_idx < 0:
        raise ValueError(
            f"could not locate header row in {file_path}: "
            f"no row contains both {field_date!r} and {field_amount!r}"
        )

    headers = [_cell_to_str(c).strip() for c in sheet.row_values(header_row_idx)]
    for r in range(header_row_idx + 1, sheet.nrows):
        # `row_values` always returns `sheet.ncols` cells (xlrd pads short
        # rows with empty strings), so headers and values are the same length.
        values = [_cell_to_str(c).strip() for c in sheet.row_values(r)]
        row = dict(zip(headers, values))
        # Skip the blank separator row most banks insert between header and body.
        if not row.get(field_date, "").strip():
            continue
        yield row


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
        # `CsvConfig.field_description` is normalised to `list[str]` by the
        # pydantic validator, so we only need the list branch.
        cols.update(self._csv.field_description)
        return frozenset(cols)

    def parse(self, file_path: str) -> Iterator[SourceTransaction]:
        csv_cfg = self._csv
        suffix = Path(file_path).suffix.lower()
        if suffix in (".xls", ".xlsx"):
            rows: Iterator[dict[str, str]] = _read_xls_rows(
                file_path, csv_cfg.field_date, csv_cfg.field_amount
            )
        else:
            # Banks ship CSVs in mixed encodings — Sparkasse exports are
            # typically cp1252/latin-1 (the BOM-prefixed UTF-8 variant only
            # landed recently). Try the configured encoding first, then walk
            # through the usual German bank fallbacks. We decode upfront so
            # a mid-file invalid byte fails fast instead of yielding partial
            # rows.
            text = _read_with_fallback(file_path, csv_cfg.encoding)
            rows = iter(
                csv.DictReader(text.splitlines(), delimiter=csv_cfg.delimiter)
            )
        for row in rows:
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

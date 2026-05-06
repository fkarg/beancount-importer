"""Cash / Telegram-log parser.

A Telegram bot logs cash expenses to a CSV with columns: `date`, `amount`,
`description`, optional `tg_id`. Amounts are stored as positive numbers
(expenses); the parser inverts the sign so that, like every other bank source,
expenses are negative and income positive.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from typing import Iterator

from beancount_importer.config import BankConfig
from beancount_importer.models import SourceTransaction
from beancount_importer.parsers.locale import parse_amount, parse_date


class CashCsvParser:
    def __init__(self, bank_config: BankConfig) -> None:
        self._cfg = bank_config
        self._csv = bank_config.csv

    @property
    def bank_key(self) -> str:
        return self._cfg.key

    @property
    def header_signature(self) -> frozenset[str]:
        return frozenset({"date", "amount", "description"})

    def parse(self, file_path: str) -> Iterator[SourceTransaction]:
        with open(file_path, encoding=self._csv.encoding, newline="") as fh:
            for row in csv.DictReader(fh, delimiter=self._csv.delimiter):
                txn = self._parse_row(row)
                if txn is not None:
                    yield txn

    def _parse_row(self, row: dict[str, str]) -> SourceTransaction | None:
        date_str = (row.get("date") or "").strip()
        amt_str = (row.get("amount") or "").strip()
        if not date_str or not amt_str:
            return None

        date_formats = self._csv.date_format or ["%Y-%m-%d"]
        booking_date = parse_date(date_str, date_formats)
        amount = parse_amount(amt_str, self._csv.amount_locale)
        if amount > Decimal(0):
            amount = -amount

        description = (row.get("description") or "").strip() or None
        tg_id = (row.get("tg_id") or "").strip()

        raw = {k: (v or "") for k, v in row.items()}
        return SourceTransaction(
            booking_date=booking_date,
            amount=amount,
            currency="EUR",
            description=description,
            payee=None,
            bank_key=self._cfg.key,
            sepa_reference=f"tg:{tg_id}" if tg_id else "",
            raw_data=raw,
        )

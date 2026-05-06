from __future__ import annotations

from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest

from beancount_importer.beancount_io.reader import read_ledger
from beancount_importer.beancount_io.writer import append_entry, format_transaction, splice_entries

FIXTURES = Path(__file__).parent / "fixtures"


# ── reader ───────────────────────────────────────────────────────────────────

class TestReadLedger:
    def test_reads_three_transactions(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert len(entries) == 3

    def test_missing_file_returns_empty(self, tmp_path: Path):
        entries = read_ledger(tmp_path / "nonexistent.bean", "Assets:B:SPK")
        assert entries == []

    def test_first_entry_date(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].date == date(2024, 1, 15)

    def test_first_entry_narration(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].narration == "Netflix Abo"

    def test_first_entry_payee(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].payee == "Netflix"

    def test_first_entry_amount(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].amount == Decimal("-15.99")

    def test_first_entry_currency(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].currency == "EUR"

    def test_source_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].source_account == "Assets:B:SPK"

    def test_target_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].target_account == "Expenses:Entertainment"

    def test_sepa_ref_in_metadata(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].metadata.get("sepa_ref") == "NETFLIX-001"

    def test_positive_amount_salary(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        salary = next(e for e in entries if "Gehalt" in e.narration)
        assert salary.amount == Decimal("3000.00")

    def test_no_entries_for_wrong_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:N26")
        assert entries == []


# ── writer: format_transaction ───────────────────────────────────────────────

class TestFormatTransaction:
    def test_basic_format(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee="Netflix",
            narration="Netflix Abo",
            postings=[
                ("Assets:B:SPK", "-15.99 EUR"),
                ("Expenses:Entertainment", "15.99 EUR"),
            ],
        )
        assert "2024-01-15 * " in text
        assert '"Netflix"' in text
        assert '"Netflix Abo"' in text
        assert "Assets:B:SPK" in text
        assert "-15.99 EUR" in text

    def test_no_payee(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Transfer",
            postings=[("Assets:B:SPK", "-100 EUR"), ("Assets:B:N26", "100 EUR")],
        )
        assert '"Transfer"' in text
        # No extra quotes for missing payee
        assert text.count('"Transfer"') == 1

    def test_metadata_included(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
            metadata={"sepa_ref": "REF-001"},
        )
        assert 'sepa_ref: "REF-001"' in text

    def test_ends_with_newline(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
        )
        assert text.endswith("\n")


# ── writer: append_entry ─────────────────────────────────────────────────────

class TestAppendEntry:
    def test_appends_to_new_file(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        append_entry("2024-01-15 * \"Netflix\" \"test\"\n  Assets:B:SPK  -15 EUR\n", target)
        assert target.exists()
        assert "Netflix" in target.read_text()

    def test_dry_run_does_not_write(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        append_entry("...", target, dry_run=True)
        assert not target.exists()

    def test_appends_to_existing(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        target.write_text("first entry\n")
        append_entry("second entry\n", target)
        content = target.read_text()
        assert "first entry" in content
        assert "second entry" in content

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "sub" / "dir" / "SPK.bean"
        append_entry("entry\n", target)
        assert target.exists()

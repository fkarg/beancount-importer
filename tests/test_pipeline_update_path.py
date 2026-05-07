"""Regression: `_persist_results` must splice update results, not drop them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer.cli import _persist_results
from beancount_importer.config import BankConfig, Config, CsvConfig
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)


def _spk_config() -> Config:
    return Config(
        banks=[
            BankConfig(
                key="spk",
                display_name="Sparkasse",
                account="Assets:B:SPK",
                file_glob="SPK_*.csv",
                output_file="SPK.bean",
                csv=CsvConfig(
                    delimiter=";",
                    field_date="Buchungstag",
                    field_amount="Betrag",
                ),
            )
        ]
    )


def _stub_bean_check(monkeypatch):
    from beancount_importer.beancount_io import writer as writer_mod

    monkeypatch.setattr(
        writer_mod.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )


def test_update_action_rewrites_matched_entry(tmp_path: Path, monkeypatch):
    bean_file = tmp_path / "SPK.bean"
    bean_file.write_text(
        '2024-01-15 * "OldPayee" "Old narration"\n'
        "  Assets:B:SPK   -10.00 EUR\n"
        "  Expenses:Unknown\n",
        encoding="utf-8",
    )
    _stub_bean_check(monkeypatch)

    txn = SourceTransaction(
        booking_date=date(2024, 1, 15),
        amount=Decimal("-10.00"),
        currency="EUR",
        payee="NewPayee",
        description="New narration",
        bank_key="spk",
    )
    entry = LedgerEntry(
        date=date(2024, 1, 15),
        payee="OldPayee",
        narration="Old narration",
        source_account="Assets:B:SPK",
        target_account="Expenses:Unknown",
        amount=Decimal("-10.00"),
        currency="EUR",
        file_path=str(bean_file),
        line_start=1,
    )
    proposal = CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Food"),),
        payee="NewPayee",
        narration="New narration",
    )
    result = ImportResult(
        source_txn=txn,
        action="update",
        matched_entry=entry,
        proposed_changes=[
            ProposedChange("payee", "OldPayee", "NewPayee"),
            ProposedChange("account", "Expenses:Unknown", "Expenses:Food"),
        ],
        proposal=proposal,
    )

    _persist_results([result], _spk_config(), tmp_path, dry_run=False)

    rewritten = bean_file.read_text(encoding="utf-8")
    assert "NewPayee" in rewritten
    assert "Expenses:Food" in rewritten
    assert "Expenses:Unknown" not in rewritten
    assert "OldPayee" not in rewritten


def test_update_with_empty_changes_is_noop(tmp_path: Path, monkeypatch):
    """A matched entry with no proposed_changes must not rewrite the file."""
    bean_file = tmp_path / "SPK.bean"
    original = (
        '2024-01-15 * "Payee" "Narr"\n'
        "  Assets:B:SPK   -10.00 EUR\n"
        "  Expenses:Food\n"
    )
    bean_file.write_text(original, encoding="utf-8")
    _stub_bean_check(monkeypatch)

    txn = SourceTransaction(
        booking_date=date(2024, 1, 15),
        amount=Decimal("-10.00"),
        currency="EUR",
        payee="Payee",
        description="Narr",
        bank_key="spk",
    )
    entry = LedgerEntry(
        date=date(2024, 1, 15),
        payee="Payee",
        narration="Narr",
        source_account="Assets:B:SPK",
        target_account="Expenses:Food",
        amount=Decimal("-10.00"),
        currency="EUR",
        file_path=str(bean_file),
        line_start=1,
    )
    proposal = CategoryProposal(
        action="categorize", postings=(Posting(account="Expenses:Food"),)
    )
    result = ImportResult(
        source_txn=txn,
        action="update",
        matched_entry=entry,
        proposed_changes=[],
        proposal=proposal,
    )

    _persist_results([result], _spk_config(), tmp_path, dry_run=False)

    assert bean_file.read_text(encoding="utf-8") == original


def test_update_dry_run_does_not_modify(tmp_path: Path, monkeypatch):
    bean_file = tmp_path / "SPK.bean"
    original = (
        '2024-01-15 * "Old" "Old"\n'
        "  Assets:B:SPK   -10.00 EUR\n"
        "  Expenses:Unknown\n"
    )
    bean_file.write_text(original, encoding="utf-8")
    _stub_bean_check(monkeypatch)

    txn = SourceTransaction(
        booking_date=date(2024, 1, 15),
        amount=Decimal("-10.00"),
        currency="EUR",
        payee="New",
        description="New",
        bank_key="spk",
    )
    entry = LedgerEntry(
        date=date(2024, 1, 15),
        payee="Old",
        narration="Old",
        source_account="Assets:B:SPK",
        target_account="Expenses:Unknown",
        amount=Decimal("-10.00"),
        currency="EUR",
        file_path=str(bean_file),
        line_start=1,
    )
    proposal = CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Food"),),
        payee="New",
        narration="New",
    )
    result = ImportResult(
        source_txn=txn,
        action="update",
        matched_entry=entry,
        proposed_changes=[ProposedChange("payee", "Old", "New")],
        proposal=proposal,
    )

    _persist_results([result], _spk_config(), tmp_path, dry_run=True)

    assert bean_file.read_text(encoding="utf-8") == original

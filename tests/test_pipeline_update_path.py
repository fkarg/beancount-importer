"""Regression: `_persist_results` must splice update results, not drop them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer.cli import _persist_new_rules, _persist_results
from beancount_importer.config import BankConfig, Config, CsvConfig
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.storage import load_rules


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
    # The post-splice validation is parse-only now; keep these integration
    # tests hermetic by forcing it to pass regardless of parser availability.
    from beancount_importer.beancount_io import writer as writer_mod

    monkeypatch.setattr(writer_mod, "_syntax_errors", lambda content: [])


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


def test_persist_new_rules_replaces_edited_rule_in_place(tmp_path: Path):
    old = CategorizationRule(
        target_account="Expenses:Old", payee_pattern="X", match_mode="contains"
    )
    new = CategorizationRule(
        target_account="Expenses:New", payee_pattern="X", match_mode="contains"
    )
    other = CategorizationRule(
        target_account="Expenses:Y", payee_pattern="Y", match_mode="contains"
    )
    txn = SourceTransaction(
        booking_date=date(2024, 1, 1), amount=Decimal("-1"), currency="EUR",
        payee="X", bank_key="spk",
    )
    result = ImportResult(
        source_txn=txn, action="new", new_rule=new, replaced_rule=old
    )
    path = tmp_path / "rules.json"
    _persist_new_rules([result], [other, old], path, dry_run=False)
    rules = load_rules(path)
    assert new in rules
    assert old not in rules
    assert other in rules  # untouched, order preserved
    assert rules.index(new) == 1  # replaced in place, not appended


def test_persist_results_isolates_a_failing_write(tmp_path: Path, monkeypatch):
    """One entry that fails to write must not abort the rest of the batch.

    Previously a single `apply_update` raise (e.g. the `__tolerances__` splice
    crash) killed the whole loop, so every later good entry was lost too. Now
    the failure is isolated and surfaced, and the good entries still land.
    """
    _stub_bean_check(monkeypatch)
    # An update whose matched entry points at a missing file → apply_update
    # raises FileNotFoundError while detecting the entry end.
    bad_txn = SourceTransaction(
        booking_date=date(2024, 1, 10),
        amount=Decimal("-5.00"),
        currency="EUR",
        payee="Bad",
        description="x",
        bank_key="spk",
    )
    bad_entry = LedgerEntry(
        date=date(2024, 1, 10),
        payee="Bad",
        narration="x",
        source_account="Assets:B:SPK",
        target_account="Expenses:Unknown",
        amount=Decimal("-5.00"),
        currency="EUR",
        file_path=str(tmp_path / "missing.bean"),
        line_start=1,
    )
    bad = ImportResult(
        source_txn=bad_txn,
        action="update",
        matched_entry=bad_entry,
        proposed_changes=[ProposedChange("payee", "Bad", "Bad2")],
        proposal=CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:Food"),),
            payee="Bad2",
        ),
    )
    good_txn = SourceTransaction(
        booking_date=date(2024, 1, 11),
        amount=Decimal("-9.00"),
        currency="EUR",
        payee="Good",
        description="y",
        bank_key="spk",
    )
    good = ImportResult(
        source_txn=good_txn,
        action="new",
        new_entry_text=(
            '2024-01-11 * "Good" "y"\n'
            "  Assets:B:SPK   -9.00 EUR\n"
            "  Expenses:Food   9.00 EUR\n"
        ),
    )

    failures = _persist_results([bad, good], _spk_config(), tmp_path, dry_run=False)

    # The good entry landed despite the earlier failure.
    assert "Good" in (tmp_path / "SPK.bean").read_text(encoding="utf-8")
    # The failure is surfaced, not swallowed.
    assert [r for r, _exc in failures] == [bad]


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

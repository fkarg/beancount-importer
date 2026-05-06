from decimal import Decimal
from datetime import date

import pytest

from beancount_importer.models import (
    SourceTransaction,
    LedgerEntry,
    Posting,
    ProposedChange,
    CategoryProposal,
    ImportResult,
)


def make_csv_txn(**kwargs) -> SourceTransaction:
    defaults = dict(
        booking_date=date(2024, 1, 15),
        amount=Decimal("-15.99"),
        currency="EUR",
        bank_key="spk",
    )
    return SourceTransaction(**(defaults | kwargs))


def make_ledger_entry(**kwargs) -> LedgerEntry:
    defaults = dict(
        date=date(2024, 1, 15),
        narration="Netflix",
        source_account="Assets:B:SPK",
        target_account="Expenses:Entertainment",
        amount=Decimal("-15.99"),
    )
    return LedgerEntry(**(defaults | kwargs))


class TestSourceTransaction:
    def test_construction(self):
        txn = make_csv_txn()
        assert txn.booking_date == date(2024, 1, 15)
        assert txn.amount == Decimal("-15.99")

    def test_frozen(self):
        txn = make_csv_txn()
        with pytest.raises(Exception):
            txn.amount = Decimal("0")  # type: ignore[misc]

    def test_amount_is_decimal(self):
        txn = make_csv_txn(amount=Decimal("42.50"))
        assert isinstance(txn.amount, Decimal)

    def test_optional_fields_default_none(self):
        txn = make_csv_txn()
        assert txn.value_date is None
        assert txn.description is None
        assert txn.payee is None
        assert txn.original_amount is None
        assert txn.original_currency is None
        assert txn.exchange_rate is None

    def test_sepa_reference_defaults_empty(self):
        txn = make_csv_txn()
        assert txn.sepa_reference == ""

    def test_raw_data_defaults_empty(self):
        txn = make_csv_txn()
        assert txn.raw_data == {}

    def test_with_sepa_reference(self):
        txn = make_csv_txn(sepa_reference="NETFLIX-001")
        assert txn.sepa_reference == "NETFLIX-001"

    def test_with_fx(self):
        txn = make_csv_txn(
            original_amount=Decimal("19.99"),
            original_currency="USD",
            exchange_rate=Decimal("0.91"),
        )
        assert txn.original_currency == "USD"
        assert txn.exchange_rate == Decimal("0.91")


class TestLedgerEntry:
    def test_construction(self):
        entry = make_ledger_entry()
        assert entry.narration == "Netflix"
        assert entry.flag == "*"

    def test_frozen(self):
        entry = make_ledger_entry()
        with pytest.raises(Exception):
            entry.narration = "changed"  # type: ignore[misc]

    def test_amount_is_decimal(self):
        entry = make_ledger_entry(amount=Decimal("100.00"))
        assert isinstance(entry.amount, Decimal)

    def test_optional_payee(self):
        entry = make_ledger_entry()
        assert entry.payee is None

    def test_metadata_defaults_empty(self):
        entry = make_ledger_entry()
        assert entry.metadata == {}

    def test_line_numbers_default_zero(self):
        entry = make_ledger_entry()
        assert entry.line_start == 0
        assert entry.line_end == 0
        assert entry.file_path == ""


class TestProposedChange:
    def test_is_named_tuple(self):
        change = ProposedChange(field="narration", old_val="Netflix", new_val="Netflix BV")
        assert change.field == "narration"
        assert change.old_val == "Netflix"
        assert change.new_val == "Netflix BV"

    def test_unpacking(self):
        change = ProposedChange(field="payee", old_val="old", new_val="new")
        field, old_val, new_val = change
        assert field == "payee"
        assert old_val == "old"
        assert new_val == "new"

    def test_equality(self):
        a = ProposedChange("narration", "old", "new")
        b = ProposedChange("narration", "old", "new")
        assert a == b


class TestCategoryProposal:
    def test_categorize_action(self):
        p = CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:Food"),),
        )
        assert p.action == "categorize"
        assert p.target_account == "Expenses:Food"

    def test_target_account_empty_when_no_postings(self):
        p = CategoryProposal(action="skip")
        assert p.target_account == ""

    def test_skip_action(self):
        p = CategoryProposal(action="skip")
        assert p.action == "skip"

    def test_frozen(self):
        p = CategoryProposal(action="skip")
        with pytest.raises(Exception):
            p.action = "categorize"  # type: ignore[misc]

    def test_defaults(self):
        p = CategoryProposal(action="skip")
        assert p.payee is None
        assert p.narration is None
        assert p.tag is None
        assert p.rule_used is None
        assert p.save_as_rule is False
        assert p.postings == ()

    def test_multi_leg_payroll_style(self):
        from decimal import Decimal as D
        p = CategoryProposal(
            action="categorize",
            postings=(
                Posting(account="Income:Salary", amount=D("-3000.00")),
                Posting(account="Expenses:Tax", amount=D("800.00")),
                Posting(account="Expenses:Social", amount=D("400.00")),
            ),
        )
        assert len(p.postings) == 3
        assert p.target_account == "Income:Salary"


class TestImportResult:
    def test_new_action(self):
        txn = make_csv_txn()
        result = ImportResult(source_txn=txn, action="new", new_entry_text="2024-01-15 * ...")
        assert result.action == "new"
        assert result.matched_entry is None

    def test_frozen(self):
        txn = make_csv_txn()
        result = ImportResult(source_txn=txn, action="skip")
        with pytest.raises(Exception):
            result.action = "new"  # type: ignore[misc]

    def test_proposed_changes_default_empty(self):
        txn = make_csv_txn()
        result = ImportResult(source_txn=txn, action="skip")
        assert result.proposed_changes == []

    def test_with_proposed_changes(self):
        txn = make_csv_txn()
        change = ProposedChange("narration", "old", "new")
        result = ImportResult(source_txn=txn, action="update", proposed_changes=[change])
        assert len(result.proposed_changes) == 1
        assert result.proposed_changes[0].field == "narration"

    def test_is_replay_defaults_false(self):
        txn = make_csv_txn()
        result = ImportResult(source_txn=txn, action="skip")
        assert result.is_replay is False

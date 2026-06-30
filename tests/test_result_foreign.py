"""Foreign-currency `@@` rendering on the new-entry path (`_result`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from beancount_importer.config import BankConfig, CsvConfig
from beancount_importer.models import CategoryProposal, Posting, SourceTransaction
from beancount_importer.pipeline._result import _foreign_price_str, _format_new_entry


def _bank() -> BankConfig:
    return BankConfig(
        key="paypal",
        display_name="PayPal",
        account="Assets:B:PayPal",
        file_glob="PayPal_*.csv",
        output_file="PayPal.bean",
        csv=CsvConfig(
            delimiter=",",
            date_format=["%Y-%m-%d"],
            amount_locale="en",
            field_date="Date",
            field_amount="Net",
        ),
    )


def _foreign_txn(amount: str = "-25.03") -> SourceTransaction:
    # Collapsed PayPal foreign purchase: home leg EUR, original USD magnitude.
    return SourceTransaction(
        booking_date=date(2024, 3, 25),
        amount=Decimal(amount),
        currency="EUR",
        payee="Uber Technologies, Inc",
        description="PreApproved Payment Bill User Payment",
        bank_key="paypal",
        original_amount=Decimal("25.95"),
        original_currency="USD",
    )


def _expense_proposal() -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Transport:Taxi"),),
    )


class TestForeignPriceStr:
    def test_purchase_counter_leg_is_positive(self):
        assert (
            _foreign_price_str(_foreign_txn())
            == "25.95 USD @@ 25.03 EUR"
        )

    def test_refund_counter_leg_is_negative(self):
        # A foreign refund credits PayPal (+home); the counter-leg flips sign.
        txn = _foreign_txn(amount="25.03")
        assert _foreign_price_str(txn) == "-25.95 USD @@ 25.03 EUR"

    def test_none_without_original_amount(self):
        txn = _foreign_txn().model_copy(update={"original_amount": None})
        assert _foreign_price_str(txn) is None

    def test_none_without_original_currency(self):
        txn = _foreign_txn().model_copy(update={"original_currency": None})
        assert _foreign_price_str(txn) is None

    def test_none_for_negative_original_amount_n26_convention(self):
        # The generic/N26 parser stores a signed (negative) original amount;
        # that must NOT trigger `@@` — N26 multi-currency stays deferred.
        txn = _foreign_txn().model_copy(update={"original_amount": Decimal("-10.00")})
        assert _foreign_price_str(txn) is None


class TestFormatNewEntryForeign:
    def test_emits_priced_counter_leg(self):
        text = _format_new_entry(_bank(), _foreign_txn(), _expense_proposal())
        assert "Assets:B:PayPal" in text
        assert "-25.03 EUR" in text
        assert "Expenses:Transport:Taxi" in text
        assert "25.95 USD @@ 25.03 EUR" in text

    def test_plain_when_no_foreign(self):
        txn = _foreign_txn().model_copy(
            update={"original_amount": None, "original_currency": None}
        )
        text = _format_new_entry(_bank(), txn, _expense_proposal())
        assert "@@" not in text
        # Counter-leg left for beancount to infer (no explicit amount).
        assert "Expenses:Transport:Taxi\n" in text or text.rstrip().endswith(
            "Expenses:Transport:Taxi"
        )

    def test_no_pricing_when_multiple_postings(self):
        # Defensive: a multi-leg proposal (e.g. payroll) is not a simple
        # foreign purchase; `@@` pricing must not hijack it.
        proposal = CategoryProposal(
            action="categorize",
            postings=(
                Posting(account="Expenses:Transport:Taxi"),
                Posting(account="Expenses:Fees", amount=Decimal("1.00")),
            ),
        )
        text = _format_new_entry(_bank(), _foreign_txn(), proposal)
        assert "@@" not in text

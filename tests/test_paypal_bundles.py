"""PayPal post-parse bundle collapsing + the `_parse_all_inputs` hook."""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer.config import BankConfig, CsvConfig
from beancount_importer.models import SourceTransaction
from beancount_importer.pipeline._paypal_bundles import collapse_paypal_bundles
from beancount_importer.pipeline._shared import _parse_all_inputs


def _tx(
    desc: str,
    txnid: str,
    reftxn: str,
    amount: str,
    currency: str = "EUR",
    payee: str | None = None,
) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 25),
        amount=Decimal(amount),
        currency=currency,
        payee=payee,
        description=desc,
        bank_key="paypal",
        sepa_reference=txnid,
        raw_data={
            "Description": desc,
            "Transaction ID": txnid,
            "Reference Txn ID": reftxn,
            "Currency": currency,
        },
    )


def _fx_bundle() -> list[SourceTransaction]:
    return [
        _tx("PreApproved Payment Bill User Payment", "PAY1", "EXT0", "-25.95", "USD", "Uber"),
        _tx("Bank Deposit to PP Account", "DEP1", "PAY1", "25.03", "EUR"),
        _tx("General Currency Conversion", "GCC_EUR", "PAY1", "-25.03", "EUR"),
        _tx("General Currency Conversion", "GCC_USD", "PAY1", "25.95", "USD"),
    ]


class TestCurrencyConversion:
    def test_collapses_bundle_into_one_priced_txn(self):
        out = collapse_paypal_bundles(_fx_bundle())
        # payment (collapsed) + funding deposit; both GCC legs suppressed.
        assert len(out) == 2
        pay = next(t for t in out if t.payee == "Uber")
        assert pay.amount == Decimal("-25.03")
        assert pay.currency == "EUR"
        assert pay.original_amount == Decimal("25.95")
        assert pay.original_currency == "USD"

    def test_funding_deposit_survives(self):
        out = collapse_paypal_bundles(_fx_bundle())
        dep = next(t for t in out if t.sepa_reference == "DEP1")
        assert dep.amount == Decimal("25.03")
        assert dep.original_amount is None

    def test_gcc_legs_dropped(self):
        out = collapse_paypal_bundles(_fx_bundle())
        refs = {t.sepa_reference for t in out}
        assert refs == {"PAY1", "DEP1"}

    def test_incomplete_bundle_untouched(self):
        # Only the home GCC leg present → cannot collapse; pass through.
        txns = [
            _tx("PreApproved Payment Bill User Payment", "PAY1", "EXT0", "-25.95", "USD", "Uber"),
            _tx("General Currency Conversion", "GCC_EUR", "PAY1", "-25.03", "EUR"),
        ]
        out = collapse_paypal_bundles(txns)
        assert len(out) == 2
        pay = next(t for t in out if t.payee == "Uber")
        assert pay.amount == Decimal("-25.95")
        assert pay.original_amount is None

    def test_orphan_gcc_without_parent_passes_through(self):
        # GCC leg with no Transaction ID and no Reference Txn ID → left alone.
        txns = [_tx("General Currency Conversion", "", "", "-1.00", "EUR")]
        out = collapse_paypal_bundles(txns)
        assert len(out) == 1
        assert out[0].original_amount is None

    def test_domestic_payment_unaffected(self):
        txns = [_tx("Mobile Payment", "PAY9", "EXT9", "-12.50", "EUR", "Bakery")]
        out = collapse_paypal_bundles(txns)
        assert len(out) == 1
        assert out[0].amount == Decimal("-12.50")
        assert out[0].original_amount is None


class TestHoldReversal:
    def test_matched_pair_dropped(self):
        txns = [
            _tx("Account Hold for Open Authorization", "HOLD1", "EXT", "-15.99", "EUR"),
            _tx("Reversal of General Account Hold", "REV1", "HOLD1", "15.99", "EUR"),
            _tx("Mobile Payment", "PAY1", "EXT", "-15.99", "EUR", "Spotify"),
        ]
        out = collapse_paypal_bundles(txns)
        assert [t.sepa_reference for t in out] == ["PAY1"]

    def test_unpaired_hold_kept(self):
        txns = [_tx("Account Hold for Open Authorization", "HOLD1", "EXT", "-15.99", "EUR")]
        out = collapse_paypal_bundles(txns)
        assert [t.sepa_reference for t in out] == ["HOLD1"]

    def test_non_mirror_amounts_kept(self):
        txns = [
            _tx("Account Hold for Open Authorization", "HOLD1", "EXT", "-15.99", "EUR"),
            _tx("Reversal of General Account Hold", "REV1", "HOLD1", "10.00", "EUR"),
        ]
        out = collapse_paypal_bundles(txns)
        assert {t.sepa_reference for t in out} == {"HOLD1", "REV1"}

    def test_reversal_referencing_non_hold_kept(self):
        txns = [
            _tx("Mobile Payment", "PAY1", "EXT", "-15.99", "EUR", "Shop"),
            _tx("Reversal of General Account Hold", "REV1", "PAY1", "15.99", "EUR"),
        ]
        out = collapse_paypal_bundles(txns)
        assert {t.sepa_reference for t in out} == {"PAY1", "REV1"}

    def test_reversal_with_missing_parent_kept(self):
        txns = [_tx("Reversal of General Account Hold", "REV1", "GONE", "15.99", "EUR")]
        out = collapse_paypal_bundles(txns)
        assert [t.sepa_reference for t in out] == ["REV1"]


# ── Pipeline hook ─────────────────────────────────────────────────────────────

_FX_CSV = textwrap.dedent("""\
    Date,Description,Currency,Net,Name,Transaction ID,Reference Txn ID
    2024-03-25,PreApproved Payment Bill User Payment,USD,-25.95,Uber,PAY1,EXT0
    2024-03-25,Bank Deposit to PP Account,EUR,25.03,,DEP1,PAY1
    2024-03-25,General Currency Conversion,EUR,-25.03,,GCC_EUR,PAY1
    2024-03-25,General Currency Conversion,USD,25.95,,GCC_USD,PAY1
""")


def _paypal_bank() -> BankConfig:
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
            field_currency="Currency",
            field_payee="Name",
            field_description="Description",
            field_sepa_reference="Transaction ID",
        ),
    )


def test_parse_all_inputs_collapses_paypal(tmp_path: Path):
    (tmp_path / "PayPal_2024.csv").write_text(_FX_CSV)
    txns = _parse_all_inputs([_paypal_bank()], tmp_path, None, None)
    # 4 raw rows → collapsed payment + funding deposit.
    assert len(txns) == 2
    pay = next(t for t in txns if t.payee == "Uber")
    assert pay.amount == Decimal("-25.03")
    assert pay.currency == "EUR"
    assert pay.original_amount == Decimal("25.95")
    assert pay.original_currency == "USD"

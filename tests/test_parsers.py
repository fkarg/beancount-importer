from __future__ import annotations

import textwrap
from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, assume
from hypothesis import strategies as st

from beancount_importer.parsers.locale import parse_amount_de, parse_amount_en, parse_date, parse_amount
from beancount_importer.parsers.base import AbstractParser, Parser
from beancount_importer.parsers.generic import GenericCsvParser
from beancount_importer.config import Config, BankConfig, CsvConfig


# ── locale: parse_date ──────────────────────────────────────────────────────

class TestParseDate:
    def test_iso_format(self):
        assert parse_date("2024-01-15", ["%Y-%m-%d"]) == date(2024, 1, 15)

    def test_german_short(self):
        assert parse_date("15.01.24", ["%d.%m.%y"]) == date(2024, 1, 15)

    def test_german_long(self):
        assert parse_date("15.01.2024", ["%d.%m.%Y"]) == date(2024, 1, 15)

    def test_tries_formats_in_order(self):
        result = parse_date("15.01.24", ["%d.%m.%y", "%d.%m.%Y"])
        assert result == date(2024, 1, 15)

    def test_falls_through_to_second_format(self):
        result = parse_date("15.01.2024", ["%d.%m.%y", "%d.%m.%Y"])
        assert result == date(2024, 1, 15)

    def test_strips_whitespace(self):
        assert parse_date("  2024-01-15  ", ["%Y-%m-%d"]) == date(2024, 1, 15)

    def test_no_matching_format_raises(self):
        with pytest.raises(ValueError):
            parse_date("2024/01/15", ["%Y-%m-%d"])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_date("", ["%Y-%m-%d"])


# ── locale: parse_amount_de ─────────────────────────────────────────────────

class TestParseAmountDe:
    def test_simple(self):
        assert parse_amount_de("1234,56") == Decimal("1234.56")

    def test_with_thousand_separator(self):
        assert parse_amount_de("1.234,56") == Decimal("1234.56")

    def test_negative(self):
        assert parse_amount_de("-42,00") == Decimal("-42.00")

    def test_negative_with_separator(self):
        assert parse_amount_de("-1.234,56") == Decimal("-1234.56")

    def test_zero(self):
        assert parse_amount_de("0,00") == Decimal("0.00")

    def test_strips_whitespace(self):
        assert parse_amount_de("  15,99  ") == Decimal("15.99")

    def test_nbsp_stripped(self):
        assert parse_amount_de("1\xa0234,56") == Decimal("1234.56")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_amount_de("")


@given(
    units=st.integers(min_value=0, max_value=999_999),
    cents=st.integers(min_value=0, max_value=99),
    negative=st.booleans(),
)
def test_parse_amount_de_roundtrip(units: int, cents: int, negative: bool):
    """Any valid German amount string round-trips through parse_amount_de."""
    cents_str = f"{cents:02d}"
    if units >= 1000:
        # format with thousand separator
        units_str = f"{units:,}".replace(",", ".")
    else:
        units_str = str(units)
    raw = f"{units_str},{cents_str}"
    if negative:
        raw = f"-{raw}"
    result = parse_amount_de(raw)
    expected = Decimal(f"{units}.{cents_str}")
    if negative:
        expected = -expected
    assert result == expected


# ── locale: parse_amount_en ─────────────────────────────────────────────────

class TestParseAmountEn:
    def test_simple(self):
        assert parse_amount_en("1234.56") == Decimal("1234.56")

    def test_with_thousand_separator(self):
        assert parse_amount_en("1,234.56") == Decimal("1234.56")

    def test_negative(self):
        assert parse_amount_en("-42.00") == Decimal("-42.00")

    def test_zero(self):
        assert parse_amount_en("0.00") == Decimal("0.00")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_amount_en("")


@given(
    units=st.integers(min_value=0, max_value=999_999),
    cents=st.integers(min_value=0, max_value=99),
    negative=st.booleans(),
)
def test_parse_amount_en_roundtrip(units: int, cents: int, negative: bool):
    cents_str = f"{cents:02d}"
    raw = f"{units}.{cents_str}"
    if negative:
        raw = f"-{raw}"
    result = parse_amount_en(raw)
    expected = Decimal(f"{units}.{cents_str}")
    if negative:
        expected = -expected
    assert result == expected


class TestParseAmountDispatch:
    def test_de_locale(self):
        assert parse_amount("1.234,56", "de") == Decimal("1234.56")

    def test_en_locale(self):
        assert parse_amount("1,234.56", "en") == Decimal("1234.56")

    def test_unknown_locale_defaults_en(self):
        assert parse_amount("42.00", "fr") == Decimal("42.00")


# ── GenericCsvParser ─────────────────────────────────────────────────────────

SPK_CSV = textwrap.dedent("""\
    Buchungstag;Valutadatum;Beguenstigter/Zahlungspflichtiger;Buchungstext;Verwendungszweck;Betrag;Waehrung;Kundenreferenz (End-to-End)
    15.01.24;15.01.24;Netflix;"Lastschrift";"Netflix Abo";-15,99;EUR;NETFLIX-001
    16.01.24;16.01.24;Rewe;"Kartenzahlung";"REWE Filiale";-42,50;EUR;
    17.01.24;17.01.24;Arbeitgeber;"Gutschrift";"Gehalt";3000,00;EUR;SALARY-JAN
""")

N26_CSV = textwrap.dedent("""\
    Date,Payee,Account number,Transaction type,Payment reference,Amount (EUR),Amount (Foreign Currency),Type Foreign Currency,Exchange Rate
    2024-01-15,Netflix,,Outgoing Transfer,Netflix subscription,-15.99,,,
    2024-01-16,Rewe,,MasterCard,,-42.5,,,
    2024-01-17,Employer,,Incoming Transfer,Salary,3000.0,,,
""")


def _make_spk_config(tmp_path: Path) -> BankConfig:
    toml = textwrap.dedent("""\
        [[banks]]
        key = "spk"
        display_name = "Sparkasse"
        account = "Assets:B:SPK"
        file_glob = "SPK_*.CSV"
        output_file = "transactions/{year}/SPK.bean"

        [banks.csv]
        delimiter = ";"
        encoding = "utf-8"
        date_format = ["%d.%m.%y", "%d.%m.%Y"]
        amount_locale = "de"
        skip_zero_amounts = true
        field_date = "Buchungstag"
        field_value_date = "Valutadatum"
        field_amount = "Betrag"
        field_currency = "Waehrung"
        field_payee = "Beguenstigter/Zahlungspflichtiger"
        field_description = ["Verwendungszweck", "Buchungstext"]
        field_sepa_reference = "Kundenreferenz (End-to-End)"
    """)
    p = tmp_path / "cfg.toml"
    p.write_text(toml)
    return Config.load(p).banks[0]


def _make_n26_config(tmp_path: Path) -> BankConfig:
    toml = textwrap.dedent("""\
        [[banks]]
        key = "n26"
        display_name = "N26"
        account = "Assets:B:N26"
        file_glob = "n26-*.csv"
        output_file = "transactions/{year}/N26.bean"

        [banks.csv]
        field_date = "Date"
        field_amount = "Amount (EUR)"
        field_payee = "Payee"
        field_description = "Transaction type"
    """)
    p = tmp_path / "n26_cfg.toml"
    p.write_text(toml)
    return Config.load(p).banks[0]


@pytest.fixture
def spk_csv(tmp_path: Path) -> Path:
    p = tmp_path / "spk.csv"
    p.write_text(SPK_CSV)
    return p


@pytest.fixture
def n26_csv(tmp_path: Path) -> Path:
    p = tmp_path / "n26.csv"
    p.write_text(N26_CSV)
    return p


class TestGenericCsvParserSPK:
    def test_parses_three_rows(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert len(txns) == 3

    def test_bank_key(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        assert parser.bank_key == "spk"

    def test_booking_date(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[0].booking_date == date(2024, 1, 15)

    def test_amount_decimal(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[0].amount == Decimal("-15.99")

    def test_positive_amount(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[2].amount == Decimal("3000.00")

    def test_payee(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[0].payee == "Netflix"

    def test_description_joined(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        # field_description = ["Verwendungszweck", "Buchungstext"] joined
        assert "Netflix Abo" in (txns[0].description or "")
        assert "Lastschrift" in (txns[0].description or "")

    def test_sepa_reference(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[0].sepa_reference == "NETFLIX-001"

    def test_empty_sepa_reference(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[1].sepa_reference == ""

    def test_currency(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[0].currency == "EUR"

    def test_raw_data_preserved(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert "Buchungstag" in txns[0].raw_data

    def test_header_signature(self, tmp_path: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        sig = parser.header_signature
        assert "Buchungstag" in sig
        assert "Betrag" in sig


class TestGenericCsvParserN26:
    def test_parses_three_rows(self, tmp_path: Path, n26_csv: Path):
        parser = GenericCsvParser(_make_n26_config(tmp_path))
        txns = list(parser.parse(str(n26_csv)))
        assert len(txns) == 3

    def test_iso_date(self, tmp_path: Path, n26_csv: Path):
        parser = GenericCsvParser(_make_n26_config(tmp_path))
        txns = list(parser.parse(str(n26_csv)))
        assert txns[0].booking_date == date(2024, 1, 15)

    def test_negative_amount(self, tmp_path: Path, n26_csv: Path):
        parser = GenericCsvParser(_make_n26_config(tmp_path))
        txns = list(parser.parse(str(n26_csv)))
        assert txns[0].amount == Decimal("-15.99")

    def test_bank_key(self, tmp_path: Path, n26_csv: Path):
        parser = GenericCsvParser(_make_n26_config(tmp_path))
        assert parser.bank_key == "n26"


class TestSkipZeroAmounts:
    def test_skips_zero(self, tmp_path: Path):
        csv_content = textwrap.dedent("""\
            Date,Amount
            2024-01-15,0.00
            2024-01-16,-10.00
        """)
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(csv_content)

        toml = textwrap.dedent("""\
            [[banks]]
            key = "x"
            display_name = "X"
            account = "Assets:B:X"
            file_glob = "*.csv"
            output_file = "x.bean"

            [banks.csv]
            skip_zero_amounts = true
            field_date = "Date"
            field_amount = "Amount"
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p).banks[0]
        parser = GenericCsvParser(cfg)
        txns = list(parser.parse(str(csv_file)))
        assert len(txns) == 1
        assert txns[0].amount == Decimal("-10.00")


class TestSkipRowWhere:
    def test_skips_matching_rows(self, tmp_path: Path):
        csv_content = textwrap.dedent("""\
            Date,Amount,Type
            2024-01-15,-10.00,PURCHASE
            2024-01-16,0.00,ABSCHLUSS
        """)
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(csv_content)

        toml = textwrap.dedent("""\
            [[banks]]
            key = "x"
            display_name = "X"
            account = "Assets:B:X"
            file_glob = "*.csv"
            output_file = "x.bean"

            [banks.csv]
            field_date = "Date"
            field_amount = "Amount"
            skip_row_where = { "Type" = "ABSCHLUSS" }
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p).banks[0]
        parser = GenericCsvParser(cfg)
        txns = list(parser.parse(str(csv_file)))
        assert len(txns) == 1
        assert txns[0].amount == Decimal("-10.00")


class TestParserProtocol:
    def test_generic_satisfies_protocol(self, tmp_path: Path):
        cfg = _make_spk_config(tmp_path)
        parser = GenericCsvParser(cfg)
        assert isinstance(parser, Parser)


# ── PayPal parser ────────────────────────────────────────────────────────────

from beancount_importer.parsers.paypal import PayPalParser
from beancount_importer.parsers.cash import CashCsvParser


PAYPAL_CSV = textwrap.dedent("""\
    Date,Time,Description,Currency,Net,Balance Impact,Name,Transaction ID
    2024-01-15,10:23:45,Amazon order,EUR,-25.50,Completed,Amazon Payments,TX-001
    2024-01-16,12:00:00,Auth hold,EUR,0.00,Memo,Some Vendor,TX-002
    2024-01-17,09:00:00,Income,EUR,100.00,Completed,Customer GmbH,TX-003
""")


def _make_paypal_config(tmp_path: Path) -> BankConfig:
    toml = textwrap.dedent("""\
        [[banks]]
        key = "paypal"
        display_name = "PayPal"
        account = "Assets:B:PayPal"
        file_glob = "PayPal_*.csv"
        output_file = "transactions/{year}/PayPal.bean"

        [banks.csv]
        delimiter = ","
        date_format = ["%Y-%m-%d"]
        amount_locale = "en"
        field_date = "Date"
        field_amount = "Net"
    """)
    p = tmp_path / "pp_cfg.toml"
    p.write_text(toml)
    return Config.load(p).banks[0]


class TestPayPalParser:
    def test_skips_memo_rows(self, tmp_path: Path):
        f = tmp_path / "pp.csv"
        f.write_text(PAYPAL_CSV)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 2

    def test_parses_amount_and_payee(self, tmp_path: Path):
        f = tmp_path / "pp.csv"
        f.write_text(PAYPAL_CSV)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].amount == Decimal("-25.50")
        assert txns[0].payee == "Amazon Payments"
        assert txns[0].sepa_reference == "TX-001"

    def test_keeps_time_in_raw_data(self, tmp_path: Path):
        f = tmp_path / "pp.csv"
        f.write_text(PAYPAL_CSV)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].raw_data.get("_time") == "10:23:45"

    def test_german_columns(self, tmp_path: Path):
        # PayPal exports come in two locales; both should parse without
        # changing the bank config.
        de_csv = textwrap.dedent("""\
            Datum,Zeit,Betreff,Währung,Netto,Balance Impact,Name,Transaktionscode
            2024-01-15,10:00:00,Amazon,EUR,-25.50,Completed,Amazon,TX-001
        """)
        f = tmp_path / "pp_de.csv"
        f.write_text(de_csv)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 1
        assert txns[0].amount == Decimal("-25.50")


# ── Cash parser ──────────────────────────────────────────────────────────────

CASH_CSV = textwrap.dedent("""\
    date,amount,description,tg_id
    2024-01-15,5.50,Coffee,123
    2024-01-16,12.00,Lunch,
""")


def _make_cash_config(tmp_path: Path) -> BankConfig:
    toml = textwrap.dedent("""\
        [[banks]]
        key = "cash"
        display_name = "Cash"
        account = "Assets:Cash"
        file_glob = "cash.csv"
        output_file = "transactions/{year}/Cash.bean"

        [banks.csv]
        delimiter = ","
        date_format = ["%Y-%m-%d"]
        amount_locale = "en"
        field_date = "date"
        field_amount = "amount"
    """)
    p = tmp_path / "cash_cfg.toml"
    p.write_text(toml)
    return Config.load(p).banks[0]


class TestCashCsvParser:
    def test_parses_two_rows(self, tmp_path: Path):
        f = tmp_path / "cash.csv"
        f.write_text(CASH_CSV)
        parser = CashCsvParser(_make_cash_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 2

    def test_inverts_sign(self, tmp_path: Path):
        f = tmp_path / "cash.csv"
        f.write_text(CASH_CSV)
        parser = CashCsvParser(_make_cash_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].amount == Decimal("-5.50")
        assert txns[1].amount == Decimal("-12.00")

    def test_records_telegram_id(self, tmp_path: Path):
        f = tmp_path / "cash.csv"
        f.write_text(CASH_CSV)
        parser = CashCsvParser(_make_cash_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].sepa_reference == "tg:123"
        assert txns[1].sepa_reference == ""

    def test_skips_unparseable_rows(self, tmp_path: Path):
        broken = textwrap.dedent("""\
            date,amount,description,tg_id
            2024-01-15,5.50,Coffee,123
            ,not_a_number,,
            2024-01-16,7.00,Tea,
        """)
        f = tmp_path / "broken.csv"
        f.write_text(broken)
        parser = CashCsvParser(_make_cash_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 2

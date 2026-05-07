from __future__ import annotations

import textwrap
from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beancount_importer.parsers.locale import parse_amount_de, parse_amount_en, parse_date, parse_amount
from beancount_importer.parsers.base import Parser
from beancount_importer.parsers.generic import GenericCsvParser
from beancount_importer.config import Config, BankConfig


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
    # Format with thousand separator (German uses dots) when >= 1000.
    units_str = f"{units:,}".replace(",", ".") if units >= 1000 else str(units)
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


class TestParseAmountCurrencyDecoration:
    """The Zinia .xls export ships amounts as `+184,90 €` and `-43,22 €`; the
    Sparkasse cp1252 CSVs sometimes emit a leading `EUR` token. Decorations
    must round-trip cleanly through both locales."""

    def test_trailing_euro_symbol_de(self):
        assert parse_amount_de("184,90 €") == Decimal("184.90")

    def test_trailing_euro_negative_de(self):
        assert parse_amount_de("-43,22 €") == Decimal("-43.22")

    def test_trailing_eur_token_de(self):
        assert parse_amount_de("99,00 EUR") == Decimal("99.00")

    def test_leading_dollar_en(self):
        assert parse_amount_en("$1,234.56") == Decimal("1234.56")

    def test_leading_pound_en(self):
        assert parse_amount_en("£10.00") == Decimal("10.00")

    def test_explicit_plus_sign_de(self):
        assert parse_amount_de("+12,34") == Decimal("12.34")

    def test_explicit_plus_sign_en(self):
        assert parse_amount_en("+12.34") == Decimal("12.34")

    def test_only_currency_symbol_raises(self):
        # After stripping the symbol, the remaining string is empty —
        # this is malformed input, not a zero amount.
        with pytest.raises(ValueError):
            parse_amount_de("€")

    def test_garbage_de_raises(self):
        with pytest.raises(ValueError):
            parse_amount_de("not-a-number")

    def test_garbage_en_raises(self):
        with pytest.raises(ValueError):
            parse_amount_en("not-a-number")


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


class TestGenericCsvParserExtras:
    """Coverage for the less-trodden generic-parser branches: value-date
    column, original-amount/currency, and the empty-row skip path."""

    def test_value_date_column_parsed(self, tmp_path: Path, spk_csv: Path):
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(spk_csv)))
        assert txns[0].value_date == date(2024, 1, 15)

    def test_value_date_blank_yields_none(self, tmp_path: Path):
        # Bank can leave Valutadatum empty for a pending booking — the parser
        # must keep value_date=None rather than re-using the booking date.
        text = textwrap.dedent("""\
            Buchungstag;Valutadatum;Beguenstigter/Zahlungspflichtiger;Buchungstext;Verwendungszweck;Betrag;Waehrung;Kundenreferenz (End-to-End)
            15.01.24;;Netflix;"L";"X";-1,00;EUR;
        """)
        f = tmp_path / "spk_vd_empty.csv"
        f.write_text(text)
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].value_date is None

    def test_blank_amount_row_skipped(self, tmp_path: Path):
        # First row has an empty amount column — the parser skips it.
        text = textwrap.dedent("""\
            Buchungstag;Valutadatum;Beguenstigter/Zahlungspflichtiger;Buchungstext;Verwendungszweck;Betrag;Waehrung;Kundenreferenz (End-to-End)
            15.01.24;15.01.24;Foo;"L";"X";;EUR;
            16.01.24;16.01.24;Bar;"L";"Y";-2,00;EUR;
        """)
        f = tmp_path / "spk_blank.csv"
        f.write_text(text)
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 1
        assert txns[0].payee == "Bar"

    def test_default_currency_when_column_blank(self, tmp_path: Path):
        # `Waehrung` column exists but a row leaves it blank — parser must
        # default to EUR rather than failing or producing an empty currency.
        text = textwrap.dedent("""\
            Buchungstag;Valutadatum;Beguenstigter/Zahlungspflichtiger;Buchungstext;Verwendungszweck;Betrag;Waehrung;Kundenreferenz (End-to-End)
            15.01.24;15.01.24;Foo;"L";"X";-1,00;;
        """)
        f = tmp_path / "spk_no_curr.csv"
        f.write_text(text)
        parser = GenericCsvParser(_make_spk_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].currency == "EUR"

    def test_original_amount_and_currency(self, tmp_path: Path):
        toml = textwrap.dedent("""\
            [[banks]]
            key = "n26"
            display_name = "N26"
            account = "Assets:B:N26"
            file_glob = "N26_*.csv"
            output_file = "n26.bean"

            [banks.csv]
            delimiter = ","
            date_format = ["%Y-%m-%d"]
            amount_locale = "en"
            field_date = "Booking Date"
            field_amount = "Amount (EUR)"
            field_original_amount = "Original Amount"
            field_original_currency = "Original Currency"
        """)
        cfg_path = tmp_path / "n26.toml"
        cfg_path.write_text(toml)
        cfg = Config.load(cfg_path).banks[0]
        text = textwrap.dedent("""\
            Booking Date,Amount (EUR),Original Amount,Original Currency
            2024-01-15,-9.50,-10.00,USD
            2024-01-16,-1.00,,
        """)
        f = tmp_path / "n26.csv"
        f.write_text(text)
        parser = GenericCsvParser(cfg)
        txns = list(parser.parse(str(f)))
        assert txns[0].original_amount == Decimal("-10.00")
        assert txns[0].original_currency == "USD"
        assert txns[1].original_amount is None
        assert txns[1].original_currency is None


class TestGenericCsvParserEncoding:
    """Sparkasse / N26 ship CSVs in cp1252 / iso-8859-1 about as often as utf-8.
    The fallback chain must cope and the failure case must surface as a
    `UnicodeDecodeError` with the attempted encodings in the message."""

    def test_cp1252_csv_decoded_via_fallback(self, tmp_path: Path):
        # Write a cp1252-encoded file with an Ümlaut in the description; declare
        # encoding="utf-8" in the config to force the fallback chain to fire.
        text = textwrap.dedent("""\
            Buchungstag;Valutadatum;Beguenstigter/Zahlungspflichtiger;Buchungstext;Verwendungszweck;Betrag;Waehrung;Kundenreferenz (End-to-End)
            15.01.24;15.01.24;Müller;"L";"Über uns";-1,00;EUR;
        """)
        f = tmp_path / "spk_cp1252.csv"
        # Use a non-overlapping byte-level write to ensure the file isn't valid utf-8.
        # \xfc is "ü" in cp1252 but invalid as a leading byte in utf-8.
        f.write_bytes(text.encode("cp1252"))

        toml = textwrap.dedent("""\
            [[banks]]
            key = "spk"
            display_name = "Sparkasse"
            account = "Assets:B:SPK"
            file_glob = "*.csv"
            output_file = "spk.bean"

            [banks.csv]
            delimiter = ";"
            encoding = "utf-8"
            date_format = ["%d.%m.%y"]
            amount_locale = "de"
            field_date = "Buchungstag"
            field_amount = "Betrag"
            field_payee = "Beguenstigter/Zahlungspflichtiger"
        """)
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text(toml)
        cfg = Config.load(cfg_path).banks[0]
        parser = GenericCsvParser(cfg)
        txns = list(parser.parse(str(f)))
        assert len(txns) == 1
        assert txns[0].payee == "Müller"

    def test_undecodable_bytes_raise_unicode_error(self, tmp_path: Path):
        # Bytes that break ALL the fallback encodings — utf-8/cp1252/iso-8859-1
        # all read single bytes, so to actually fail we'd need an invalid utf-8
        # *primary* with strict=fail logic. Instead we exercise the explicit
        # raise by patching `_FALLBACK_ENCODINGS` to a list of an encoding that
        # rejects the bytes (e.g. ascii) on bytes that aren't ASCII.
        from beancount_importer.parsers import generic as generic_mod

        f = tmp_path / "bad.csv"
        f.write_bytes(b"\xff\xfe\xfd")

        original = generic_mod._FALLBACK_ENCODINGS
        generic_mod._FALLBACK_ENCODINGS = ("ascii",)
        try:
            with pytest.raises(UnicodeDecodeError):
                generic_mod._read_with_fallback(str(f), "ascii")
        finally:
            generic_mod._FALLBACK_ENCODINGS = original


# ── XLS parsing (Zinia / Amazon Visa exports) ────────────────────────────────


def _make_zinia_xls(path: Path) -> None:
    """Build a tiny .xls workbook that mirrors the Zinia "Amazon Visa" shape:
    several preamble rows of human-readable info, then the real header, then
    one blank separator row, then data rows. `_read_xls_rows` must locate
    the header by content (`Datum` + `Betrag`) and skip the separator."""
    import xlwt  # type: ignore[import-untyped]
    book = xlwt.Workbook()
    sh = book.add_sheet("Sheet1")
    rows = [
        ["Account holder", "Felix"],
        ["Date range", "2024-01-01 — 2024-01-31"],
        ["Balance", "100,00 €"],
        [],  # blank preamble row
        ["Datum", "Beschreibung", "Betrag"],
        [],  # blank separator that Zinia injects between header and body
        ["15.01.2024", "Coffee", "-3,50 €"],
        ["16.01.2024", "Refund", "+5,00 €"],
    ]
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            sh.write(r, c, val)
    book.save(str(path))


class TestGenericCsvParserXls:
    def _zinia_config(self, tmp_path: Path) -> BankConfig:
        toml = textwrap.dedent("""\
            [[banks]]
            key = "zinia"
            display_name = "Zinia"
            account = "Liabilities:CreditCard:Zinia"
            file_glob = "*.xls"
            output_file = "zinia.bean"

            [banks.csv]
            delimiter = ","
            encoding = "utf-8"
            date_format = ["%d.%m.%Y"]
            amount_locale = "de"
            field_date = "Datum"
            field_amount = "Betrag"
            field_description = "Beschreibung"
        """)
        cfg_path = tmp_path / "zinia.toml"
        cfg_path.write_text(toml)
        return Config.load(cfg_path).banks[0]

    def test_parses_xls_with_preamble(self, tmp_path: Path):
        xls = tmp_path / "Zinia_2024.xls"
        _make_zinia_xls(xls)
        parser = GenericCsvParser(self._zinia_config(tmp_path))
        txns = list(parser.parse(str(xls)))
        assert len(txns) == 2

    def test_xls_amount_strips_currency(self, tmp_path: Path):
        xls = tmp_path / "Zinia_2024.xls"
        _make_zinia_xls(xls)
        parser = GenericCsvParser(self._zinia_config(tmp_path))
        txns = list(parser.parse(str(xls)))
        assert txns[0].amount == Decimal("-3.50")
        # Refund: positive amount (with explicit `+` sign).
        assert txns[1].amount == Decimal("5.00")

    def test_xls_missing_header_raises(self, tmp_path: Path):
        # Build a workbook with NO row containing both Datum + Betrag.
        import xlwt  # type: ignore[import-untyped]
        book = xlwt.Workbook()
        sh = book.add_sheet("S")
        sh.write(0, 0, "irrelevant header")
        sh.write(1, 0, "data")
        xls = tmp_path / "no_header.xls"
        book.save(str(xls))
        parser = GenericCsvParser(self._zinia_config(tmp_path))
        with pytest.raises(ValueError, match="could not locate header row"):
            list(parser.parse(str(xls)))

    def test_xls_short_row_padded(self, tmp_path: Path):
        # A row shorter than the header must be padded so dict construction
        # is still well-defined — exercises the `len(values) < len(headers)`
        # branch in `_read_xls_rows`.
        import xlwt  # type: ignore[import-untyped]
        book = xlwt.Workbook()
        sh = book.add_sheet("S")
        sh.write(0, 0, "Datum")
        sh.write(0, 1, "Beschreibung")
        sh.write(0, 2, "Betrag")
        sh.write(1, 0, "15.01.2024")
        # Intentionally do not write column 1 or 2 for row 1 — only writing
        # the date leaves the row "short". Then a real complete row.
        sh.write(2, 0, "16.01.2024")
        sh.write(2, 1, "Test")
        sh.write(2, 2, "-1,00 €")
        xls = tmp_path / "short_row.xls"
        book.save(str(xls))
        parser = GenericCsvParser(self._zinia_config(tmp_path))
        # Row 1 is missing the amount → skipped. Row 2 is complete.
        txns = list(parser.parse(str(xls)))
        assert len(txns) == 1
        assert txns[0].booking_date == date(2024, 1, 16)


class TestCellToStr:
    """`_cell_to_str` coerces xlrd cell types into strings the row parser
    expects. Each value-shape needs its own branch tested."""

    def test_integer_float(self):
        from beancount_importer.parsers.generic import _cell_to_str
        # xlrd returns numeric cells as floats — integers should serialize
        # without a decimal point so date parsers and amount parsers see
        # the same string they would from a CSV.
        assert _cell_to_str(2024.0) == "2024"

    def test_fractional_float(self):
        from beancount_importer.parsers.generic import _cell_to_str
        # Trailing zeros stripped, but precision preserved.
        assert _cell_to_str(1.5) == "1.5"
        assert _cell_to_str(1.25) == "1.25"

    def test_none(self):
        from beancount_importer.parsers.generic import _cell_to_str
        assert _cell_to_str(None) == ""

    def test_string_passthrough(self):
        from beancount_importer.parsers.generic import _cell_to_str
        assert _cell_to_str("Datum") == "Datum"


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

    def test_already_negative_amount_preserved(self, tmp_path: Path):
        """Cash parser inverts positive amounts, but a negative amount (a refund
        or correction) must pass through untouched."""
        text = "date,amount,description,tg_id\n2024-01-15,-2.50,refund,99\n"
        f = tmp_path / "neg.csv"
        f.write_text(text)
        parser = CashCsvParser(_make_cash_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].amount == Decimal("-2.50")

    def test_default_date_format_when_unset(self, tmp_path: Path):
        """Cash parser falls back to ISO date when the bank config doesn't
        specify a `date_format` — covers the `or ["%Y-%m-%d"]` fallback."""
        toml = textwrap.dedent("""\
            [[banks]]
            key = "cash"
            display_name = "Cash"
            account = "Assets:Cash"
            file_glob = "cash.csv"
            output_file = "cash.bean"

            [banks.csv]
            field_date = "date"
            field_amount = "amount"
            date_format = []
        """)
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text(toml)
        cfg = Config.load(cfg_path).banks[0]
        f = tmp_path / "cash.csv"
        f.write_text("date,amount,description,tg_id\n2024-01-15,1.00,Foo,\n")
        parser = CashCsvParser(cfg)
        txns = list(parser.parse(str(f)))
        assert txns[0].booking_date == date(2024, 1, 15)

    def test_bank_key_property(self, tmp_path: Path):
        parser = CashCsvParser(_make_cash_config(tmp_path))
        assert parser.bank_key == "cash"

    def test_header_signature_property(self, tmp_path: Path):
        parser = CashCsvParser(_make_cash_config(tmp_path))
        assert parser.header_signature == frozenset({"date", "amount", "description"})


# ── PayPal: empty-field and bilingual coverage ───────────────────────────────


class TestPayPalParserEdgeCases:
    def test_empty_date_row_skipped(self, tmp_path: Path):
        # First row has no date — parser skips it without raising.
        text = textwrap.dedent("""\
            Date,Time,Description,Currency,Net,Balance Impact,Name,Transaction ID
            ,,no date,EUR,-1.00,Completed,X,TX-1
            2024-01-16,00:00:00,real,EUR,-2.00,Completed,Y,TX-2
        """)
        f = tmp_path / "pp.csv"
        f.write_text(text)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 1
        assert txns[0].sepa_reference == "TX-2"

    def test_empty_amount_row_skipped(self, tmp_path: Path):
        text = textwrap.dedent("""\
            Date,Time,Description,Currency,Net,Balance Impact,Name,Transaction ID
            2024-01-15,00:00:00,no amount,EUR,,Completed,X,TX-1
            2024-01-16,00:00:00,real,EUR,-2.00,Completed,Y,TX-2
        """)
        f = tmp_path / "pp.csv"
        f.write_text(text)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert len(txns) == 1
        assert txns[0].sepa_reference == "TX-2"

    def test_row_without_time_omits_time_metadata(self, tmp_path: Path):
        text = textwrap.dedent("""\
            Date,Description,Currency,Net,Balance Impact,Name,Transaction ID
            2024-01-15,Test,EUR,-1.00,Completed,X,TX-1
        """)
        f = tmp_path / "pp.csv"
        f.write_text(text)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert "_time" not in txns[0].raw_data

    def test_default_currency_when_column_missing(self, tmp_path: Path):
        # `_first` returns "" for an unmatched key; parser falls back to EUR.
        text = textwrap.dedent("""\
            Date,Description,Net,Balance Impact,Name,Transaction ID
            2024-01-15,No currency col,-1.00,Completed,X,TX-1
        """)
        f = tmp_path / "pp.csv"
        f.write_text(text)
        parser = PayPalParser(_make_paypal_config(tmp_path))
        txns = list(parser.parse(str(f)))
        assert txns[0].currency == "EUR"

    def test_original_amount_and_currency(self, tmp_path: Path):
        # Foreign-currency PayPal rows — Net is in account currency, Original
        # captures the foreign amount the merchant invoiced.
        toml = textwrap.dedent("""\
            [[banks]]
            key = "paypal"
            display_name = "PayPal"
            account = "Assets:B:PayPal"
            file_glob = "PayPal_*.csv"
            output_file = "PayPal.bean"

            [banks.csv]
            delimiter = ","
            date_format = ["%Y-%m-%d"]
            amount_locale = "en"
            field_date = "Date"
            field_amount = "Net"
            field_original_amount = "Foreign Amount"
            field_original_currency = "Foreign Currency"
        """)
        cfg_path = tmp_path / "pp_fx.toml"
        cfg_path.write_text(toml)
        cfg = Config.load(cfg_path).banks[0]
        text = textwrap.dedent("""\
            Date,Description,Currency,Net,Balance Impact,Name,Transaction ID,Foreign Amount,Foreign Currency
            2024-01-15,Foreign,EUR,-9.50,Completed,X,TX-1,-10.00,USD
            2024-01-16,Domestic,EUR,-2.00,Completed,Y,TX-2,,
        """)
        f = tmp_path / "pp.csv"
        f.write_text(text)
        parser = PayPalParser(cfg)
        txns = list(parser.parse(str(f)))
        assert txns[0].original_amount == Decimal("-10.00")
        assert txns[0].original_currency == "USD"
        # Empty foreign columns leave the optional fields None.
        assert txns[1].original_amount is None
        assert txns[1].original_currency is None

    def test_bank_key_property(self, tmp_path: Path):
        parser = PayPalParser(_make_paypal_config(tmp_path))
        assert parser.bank_key == "paypal"

    def test_header_signature_property(self, tmp_path: Path):
        parser = PayPalParser(_make_paypal_config(tmp_path))
        sig = parser.header_signature
        # Bilingual coverage — both English and German keys must be in the signature.
        assert "Date" in sig
        assert "Datum" in sig
        assert "Net" in sig
        assert "Netto" in sig
        assert "Name" in sig

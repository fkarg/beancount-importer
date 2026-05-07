import textwrap
from pathlib import Path

import pytest

from beancount_importer.config import (
    Config,
    SkipUpdatePattern,
    TransformsConfig,
    MatchingConfig,
)


MINIMAL_TOML = textwrap.dedent("""\
    [[banks]]
    key = "spk"
    display_name = "Sparkasse"
    account = "Assets:B:SPK"
    file_glob = "SPK_*.CSV"
    output_file = "transactions/{year}/SPK.bean"

    [banks.csv]
    delimiter = ";"
    encoding = "latin-1"
    date_format = ["%d.%m.%y", "%d.%m.%Y"]
    amount_locale = "de"
    skip_zero_amounts = true
    field_date = "Buchungstag"
    field_amount = "Betrag"
    field_currency = "Waehrung"
    field_payee = "Beguenstigter/Zahlungspflichtiger"
    field_description = ["Verwendungszweck", "Buchungstext"]
    field_sepa_reference = "Kundenreferenz (End-to-End)"
""")

TWO_BANKS_TOML = MINIMAL_TOML + textwrap.dedent("""\

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


@pytest.fixture
def toml_file(tmp_path: Path) -> Path:
    p = tmp_path / "import_config.toml"
    p.write_text(MINIMAL_TOML)
    return p


@pytest.fixture
def two_banks_file(tmp_path: Path) -> Path:
    p = tmp_path / "import_config.toml"
    p.write_text(TWO_BANKS_TOML)
    return p


class TestConfigLoad:
    def test_load_returns_config(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert isinstance(cfg, Config)

    def test_one_bank_loaded(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert len(cfg.banks) == 1

    def test_two_banks_loaded(self, two_banks_file: Path):
        cfg = Config.load(two_banks_file)
        assert len(cfg.banks) == 2

    def test_bank_key(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert cfg.banks[0].key == "spk"

    def test_bank_account(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert cfg.banks[0].account == "Assets:B:SPK"

    def test_defaults(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert cfg.rules_file == ".beancount-importer/rules.json"
        assert cfg.decisions_file == ".beancount-importer/decisions.jsonl"
        assert cfg.tag_state_file == ".beancount-importer/tag_state.json"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            Config.load(tmp_path / "nonexistent.toml")

    def test_frozen(self, toml_file: Path):
        cfg = Config.load(toml_file)
        with pytest.raises(Exception):
            cfg.rules_file = "other.json"  # type: ignore[misc]


class TestBankConfig:
    def test_source_files_defaults_to_output(self, toml_file: Path):
        cfg = Config.load(toml_file)
        bank = cfg.banks[0]
        assert bank.source_files == ["transactions/{year}/SPK.bean"]

    def test_bank_lookup(self, two_banks_file: Path):
        cfg = Config.load(two_banks_file)
        bank = cfg.bank("n26")
        assert bank.display_name == "N26"

    def test_bank_lookup_missing_raises(self, toml_file: Path):
        cfg = Config.load(toml_file)
        with pytest.raises(KeyError):
            cfg.bank("unknown")


class TestCsvConfig:
    def test_delimiter(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert cfg.banks[0].csv.delimiter == ";"

    def test_date_format_list(self, toml_file: Path):
        cfg = Config.load(toml_file)
        assert cfg.banks[0].csv.date_format == ["%d.%m.%y", "%d.%m.%Y"]

    def test_date_format_coerced_from_string(self, tmp_path: Path):
        toml = textwrap.dedent("""\
            [[banks]]
            key = "x"
            display_name = "X"
            account = "Assets:B:X"
            file_glob = "*.csv"
            output_file = "x.bean"

            [banks.csv]
            date_format = "%Y-%m-%d"
            field_date = "Date"
            field_amount = "Amount"
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p)
        assert cfg.banks[0].csv.date_format == ["%Y-%m-%d"]

    def test_field_description_as_list(self, toml_file: Path):
        csv_cfg = Config.load(toml_file).banks[0].csv
        assert csv_cfg.field_description == ["Verwendungszweck", "Buchungstext"]

    def test_field_description_coerced_from_string(self, tmp_path: Path):
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
            field_description = "Memo"
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        csv_cfg = Config.load(p).banks[0].csv
        assert csv_cfg.field_description == ["Memo"]

    def test_amount_locale(self, toml_file: Path):
        assert Config.load(toml_file).banks[0].csv.amount_locale == "de"

    def test_skip_zero_amounts(self, toml_file: Path):
        assert Config.load(toml_file).banks[0].csv.skip_zero_amounts is True

    def test_optional_fields_default_none(self, toml_file: Path):
        csv_cfg = Config.load(toml_file).banks[0].csv
        assert csv_cfg.field_value_date is None
        assert csv_cfg.field_original_amount is None
        assert csv_cfg.field_original_currency is None


class TestSkipUpdatePattern:
    def test_basic(self):
        p = SkipUpdatePattern(field="payee", pattern="^Lastschrift")
        assert p.field == "payee"
        assert p.pattern == "^Lastschrift"

    def test_invalid_regex_raises(self):
        with pytest.raises(ValueError):
            SkipUpdatePattern(field="payee", pattern="[invalid")

    def test_invalid_field_raises(self):
        with pytest.raises(ValueError):
            SkipUpdatePattern(field="bogus", pattern="x")  # type: ignore[arg-type]

    def test_loaded_from_toml(self, tmp_path: Path):
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

            [[skip_update_patterns]]
            field = "narration"
            pattern = "^Lastschrift "

            [[skip_update_patterns]]
            field = "payee"
            pattern = "ABSCHLUSS"
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p)
        assert len(cfg.skip_update_patterns) == 2
        assert cfg.skip_update_patterns[0].field == "narration"
        assert cfg.skip_update_patterns[1].field == "payee"


class TestTransformsConfig:
    def test_default_includes_all_three(self):
        t = TransformsConfig()
        assert "beancount_importer.transforms.settle" in t.enabled
        assert "beancount_importer.transforms.actual" in t.enabled
        assert "beancount_importer.transforms.amortize" in t.enabled

    def test_override_via_toml(self, tmp_path: Path):
        toml = textwrap.dedent("""\
            [transforms]
            enabled = ["beancount_importer.transforms.settle"]
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p)
        assert cfg.transforms.enabled == ["beancount_importer.transforms.settle"]


class TestMatchingConfig:
    def test_defaults(self):
        m = MatchingConfig()
        assert m.min_score == 0.35
        assert m.min_delta == 0.15
        assert m.transfer_tolerance_days == 5

    def test_override_via_toml(self, tmp_path: Path):
        toml = textwrap.dedent("""\
            [matching]
            min_score = 0.5
            transfer_tolerance_days = 10
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p)
        assert cfg.matching.min_score == 0.5
        assert cfg.matching.transfer_tolerance_days == 10


class TestBankConfigSourceFilesDefault:
    """Cover the `default_source_files` validator's edge paths: when the
    incoming data is not a dict (Pydantic occasionally hands a model in)
    or when no `output_file` was supplied to derive a default from."""

    def test_pre_existing_source_files_preserved(self, tmp_path: Path):
        # When `source_files` is explicitly supplied, the default-derivation
        # block is skipped (the `not data.get("source_files")` check is False).
        toml = textwrap.dedent("""\
            [[banks]]
            key = "spk"
            display_name = "SPK"
            account = "Assets:B:SPK"
            file_glob = "SPK_*.CSV"
            output_file = "transactions/{year}/SPK.bean"
            source_files = [
                "transactions/{year}/SPK.bean",
                "transactions/{year}/Cash.bean",
            ]

            [banks.csv]
            field_date = "Buchungstag"
            field_amount = "Betrag"
        """)
        p = tmp_path / "cfg.toml"
        p.write_text(toml)
        cfg = Config.load(p)
        assert len(cfg.banks[0].source_files) == 2

    def test_revalidation_of_existing_model_skips_default(self):
        # `default_source_files` runs in `mode="before"` and receives an
        # already-validated `BankConfig` instance during nested model
        # revalidation. The validator must short-circuit (the input is not
        # a dict) rather than try to `.get(...)` on the model.
        from beancount_importer.config import BankConfig, CsvConfig

        original = BankConfig(
            key="x",
            display_name="X",
            account="Assets:X",
            file_glob="*.csv",
            output_file="x.bean",
            csv=CsvConfig(field_date="d", field_amount="a"),
        )
        # Round-trip the model through validation. Pydantic feeds the model
        # itself (not a dict) into `mode=before` validators — exercising the
        # `not isinstance(data, dict)` branch.
        revalidated = BankConfig.model_validate(original)
        assert revalidated.source_files == original.source_files

    def test_dict_without_output_file_passes_through(self):
        # If a dict without `output_file` reaches the validator, the
        # `if output is not None` branch must short-circuit cleanly. The
        # outer model_validate then surfaces a pydantic ValidationError
        # for the missing required field — NOT a KeyError leaking out
        # of `default_source_files`.
        from pydantic import ValidationError

        from beancount_importer.config import BankConfig

        with pytest.raises(ValidationError) as excinfo:
            BankConfig.model_validate({"key": "x"})
        # Both required fields surface as missing; no KeyError leaks.
        message = str(excinfo.value)
        assert "output_file" in message
        assert "KeyError" not in message

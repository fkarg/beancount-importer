"""End-to-end CLI smoke tests using typer's CliRunner.

Focuses on flows that exercise wiring (config load → pipeline → output) rather
than re-testing the pipeline. Interactive prompts are sidestepped via
`--preview`, which uses a non-interactive categorizer.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beancount_importer.cli import app


CONFIG_TOML = textwrap.dedent("""\
    rules_file = "rules.json"
    decisions_file = "decisions.jsonl"
    tag_state_file = "tag_state.json"

    [matching]
    min_score = 0.35

    [[banks]]
    key = "spk"
    display_name = "Sparkasse"
    account = "Assets:B:SPK"
    file_glob = "SPK_*.csv"
    output_file = "transactions/{year}/SPK.bean"

    [banks.csv]
    delimiter = ";"
    date_format = ["%d.%m.%y"]
    amount_locale = "de"
    field_date = "Buchungstag"
    field_amount = "Betrag"
    field_currency = "Waehrung"
    field_payee = "Beguenstigter"
    field_description = "Verwendungszweck"
""")


SPK_CSV = textwrap.dedent("""\
    Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung
    15.01.24;Netflix;Netflix Abo;-15,99;EUR
    16.01.23;Older;Old txn;-1,00;EUR
    17.01.24;Rewe;REWE Filiale;-42,50;EUR
""")


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    (tmp_path / "import_config.toml").write_text(CONFIG_TOML)
    (tmp_path / "SPK_jan.csv").write_text(SPK_CSV)
    return tmp_path


class TestImportYearPreview:
    def test_preview_writes_no_files(self, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "2024",
                "--config",
                str(project_dir / "import_config.toml"),
                "--preview",
            ],
        )
        assert result.exit_code == 0, result.output
        # No output file created in preview mode
        assert not (project_dir / "transactions" / "2024" / "SPK.bean").exists()

    def test_preview_with_year_filter(self, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "2024",
                "--config",
                str(project_dir / "import_config.toml"),
                "--preview",
                "--year-filter",
                "2024",
            ],
        )
        assert result.exit_code == 0, result.output
        # The aggregate preview reports volumes per bank rather than per-row
        # detail. The CSV has 2 rows in 2024 and 1 in 2023; filtering to 2024
        # should leave exactly 2 transactions accounted for.
        assert "new=2" in result.output

    def test_preview_shows_bean_provenance_when_ledger_present(
        self, project_dir: Path
    ):
        # An existing SPK ledger entry that no CSV row matches — the preview
        # should call it out as having no CSV source.
        year_dir = project_dir / "transactions" / "2024"
        year_dir.mkdir(parents=True)
        (year_dir / "SPK.bean").write_text(textwrap.dedent("""\
            2024-03-04 * "Cash" "no csv source"
              Assets:B:SPK  -7.50 EUR
              Expenses:Cash  7.50 EUR
        """))
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "2024",
                "--config",
                str(project_dir / "import_config.toml"),
                "--preview",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No source provenanace:" in result.output
        assert "Transactions:" in result.output


class TestInit:
    def test_init_writes_starter_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["--init"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".beancount-importer" / "config.toml").exists()
        assert (tmp_path / "transactions").is_dir()
        assert (tmp_path / "documents").is_dir()

    def test_init_does_not_overwrite(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        config_dir = tmp_path / ".beancount-importer"
        config_dir.mkdir()
        existing = config_dir / "config.toml"
        existing.write_text("# user-edited\n")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        runner.invoke(app, ["--init"])
        assert existing.read_text() == "# user-edited\n"


class TestMigrateFromLegacy:
    def test_writes_files_in_place(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".import_config.json").write_text(textwrap.dedent("""\
            {
              "rules": [
                {"pattern": "Netflix", "target_account": "Expenses:Streaming", "match_field": "payee"}
              ],
              "config": {
                "active_tag": {"tag": "trip", "mode": "always"},
                "recent_tags": ["trip", "lunch"]
              }
            }
        """))
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["--migrate"])
        assert result.exit_code == 0, result.output
        config_dir = tmp_path / ".beancount-importer"
        assert (config_dir / "config.toml").exists()
        rules_file = config_dir / "rules.json"
        assert rules_file.exists()
        assert "Netflix" in rules_file.read_text()
        tag_state = config_dir / "tag_state.json"
        assert tag_state.exists()
        assert "trip" in tag_state.read_text()
        # Legacy file is untouched
        assert (tmp_path / ".import_config.json").exists()

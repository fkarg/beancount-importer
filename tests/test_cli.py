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
    file_glob = "documents/SPK_*.csv"
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
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "SPK_jan.csv").write_text(SPK_CSV)
    (tmp_path / "transactions").mkdir()
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
        assert "CSV transactions:        2" in result.output

    def test_preview_suppresses_per_row_ticker(self, project_dir: Path):
        # The ticker prints one `…`/`✓`/`✎` line per row. Preview mode
        # already produces a per-row summary table, so doubling that
        # output up the scrollback is pure noise. Assert the ticker
        # glyphs don't show up at the start of any output line.
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
        ticker_glyphs = ("✓", "↻", "…", "✎", "⚠")
        for line in result.output.splitlines():
            stripped = line.lstrip()
            assert not stripped.startswith(ticker_glyphs), (
                f"unexpected per-row ticker in preview output: {line!r}"
            )

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
        assert "No CSV source:" in result.output
        assert "Transactions:" in result.output


class TestKeyboardInterruptSemantics:
    """Ctrl+C contract: drop the buffered decision log, drop in-flight
    .bean writes, but persist any rules the user toggled `save_as_rule`
    on before bailing — rules express durable intent that should
    outlive a rage-quit.
    """

    def test_interrupt_does_not_persist_buffered_decisions(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The pipeline buffers decisions in DecisionLog._pending and
        # only `flush()` writes them. On Ctrl+C the CLI doesn't flush,
        # so the JSONL stays empty.
        from beancount_importer.models import (
            CategoryProposal,
            ImportResult,
            Posting,
            SourceTransaction,
        )
        from datetime import date as _date
        from decimal import Decimal as _Decimal

        def fake_run(
            session, base_dir, categorize, reporter,
            *, decisions, merge_fn, results_accumulator,
        ):
            del session, base_dir, categorize, reporter, merge_fn
            txn = SourceTransaction(
                booking_date=_date(2024, 1, 15),
                amount=_Decimal("-15.99"),
                currency="EUR",
                payee="Netflix",
                description="Netflix Abo",
                bank_key="spk",
            )
            result = ImportResult(
                source_txn=txn,
                action="new",
                proposal=CategoryProposal(
                    action="categorize",
                    postings=(Posting(account="Expenses:Netflix"),),
                ),
            )
            decisions.record(txn, result)
            if results_accumulator is not None:
                results_accumulator.append(result)
            raise KeyboardInterrupt

        monkeypatch.setattr("beancount_importer.cli.run_pipeline", fake_run)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "2024",
                "--config",
                str(project_dir / "import_config.toml"),
            ],
        )
        assert result.exit_code == 130, result.output
        assert "interrupted" in result.output
        # Decisions JSONL stays empty.
        decisions_path = project_dir / "decisions.jsonl"
        if decisions_path.exists():
            assert decisions_path.read_text().strip() == ""

    def test_interrupt_persists_rules_created_before_rage_quit(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # If the user pressed [r] (save_as_rule) on a few rows then
        # hit Ctrl+C, those rules must survive — they're durable
        # intent independent of decisions/.bean writes.
        from beancount_importer.models import (
            CategoryProposal,
            ImportResult,
            Posting,
            SourceTransaction,
        )
        from beancount_importer.rules.models import CategorizationRule
        from datetime import date as _date
        from decimal import Decimal as _Decimal

        def fake_run(
            session, base_dir, categorize, reporter,
            *, decisions, merge_fn, results_accumulator,
        ):
            del session, base_dir, categorize, reporter, decisions, merge_fn
            txn = SourceTransaction(
                booking_date=_date(2024, 1, 15),
                amount=_Decimal("-15.99"),
                currency="EUR",
                payee="Netflix",
                description="Netflix Abo",
                bank_key="spk",
            )
            new_rule = CategorizationRule(
                payee_pattern="Netflix",
                target_account="Expenses:Streaming",
            )
            result = ImportResult(
                source_txn=txn,
                action="new",
                proposal=CategoryProposal(
                    action="categorize",
                    postings=(Posting(account="Expenses:Streaming"),),
                    save_as_rule=True,
                ),
                new_rule=new_rule,
            )
            assert results_accumulator is not None
            results_accumulator.append(result)
            raise KeyboardInterrupt

        monkeypatch.setattr("beancount_importer.cli.run_pipeline", fake_run)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "2024",
                "--config",
                str(project_dir / "import_config.toml"),
            ],
        )
        assert result.exit_code == 130, result.output
        assert "kept 1 new rule" in result.output
        rules_text = (project_dir / "rules.json").read_text()
        assert "Netflix" in rules_text
        assert "Expenses:Streaming" in rules_text

    def test_interrupt_with_no_pending_state_exits_cleanly(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Ctrl+C very early (before any decision recorded) — exits 130
        # with the friendly message, no traceback, no "kept N rules".
        def fake_run(
            session, base_dir, categorize, reporter,
            *, decisions, merge_fn, results_accumulator,
        ):
            del session, base_dir, categorize, reporter, decisions
            del merge_fn, results_accumulator
            raise KeyboardInterrupt

        monkeypatch.setattr("beancount_importer.cli.run_pipeline", fake_run)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "2024",
                "--config",
                str(project_dir / "import_config.toml"),
            ],
        )
        assert result.exit_code == 130, result.output
        assert "interrupted" in result.output
        assert "kept" not in result.output


class TestFlagThreading:
    def test_time_flag_threads_into_chronological_option(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        captured: dict[str, bool] = {}

        def fake_run(
            session, base_dir, categorize, reporter,
            *, decisions, merge_fn, results_accumulator,
        ):
            del base_dir, categorize, reporter, decisions, merge_fn
            captured["chronological"] = session.options.chronological
            del results_accumulator

        monkeypatch.setattr("beancount_importer.cli.run_pipeline", fake_run)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "--preview",
                "--time",
                "--config",
                str(project_dir / "import_config.toml"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert captured["chronological"] is True


class TestRichReporter:
    """Direct unit tests for the per-row reporter behaviour. The
    end-to-end test above asserts the wiring; these isolate the flag.
    """

    def _result(self):
        from datetime import date
        from decimal import Decimal

        from beancount_importer.models import (
            CategoryProposal,
            ImportResult,
            Posting,
            SourceTransaction,
        )

        return ImportResult(
            source_txn=SourceTransaction(
                booking_date=date(2024, 3, 1),
                amount=Decimal("-12.50"),
                currency="EUR",
                payee="Test",
                description="d",
                bank_key="spk",
            ),
            action="new",
            proposal=CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Food"),),
            ),
        )

    def test_quiet_suppresses_on_result(self, capsys):
        from beancount_importer.cli import RichReporter, console

        reporter = RichReporter(quiet=True)
        # Force a fresh capture buffer.
        capsys.readouterr()
        reporter.on_result(self._result())
        # Rich console writes to stdout (via the module-level singleton),
        # so capsys catches it. Quiet mode should produce nothing.
        del console  # we only need the import to ensure side effects
        out = capsys.readouterr().out
        assert out == ""

    def test_default_emits_ticker_line(self, capsys):
        from beancount_importer.cli import RichReporter

        reporter = RichReporter()
        capsys.readouterr()
        reporter.on_result(self._result())
        out = capsys.readouterr().out
        # `new` action with no rule renders the `(you)` suffix.
        assert "(you)" in out

    def test_default_progress_logs_bank_header_on_bank_change(self, capsys):
        from datetime import date

        from beancount_importer.cli import RichReporter

        reporter = RichReporter()
        capsys.readouterr()
        reporter.on_progress(1, 2, "spk", date(2024, 3, 8))
        reporter.on_progress(2, 2, "spk", date(2024, 3, 9))
        out = capsys.readouterr().out
        assert out.count("spk") == 1  # header only on first sighting of the bank
        assert "2024-03" not in out  # no date header in default mode

    def test_chronological_progress_logs_date_header_on_day_change(self, capsys):
        from datetime import date

        from beancount_importer.cli import RichReporter

        reporter = RichReporter(chronological=True)
        capsys.readouterr()
        reporter.on_progress(1, 4, "spk", date(2024, 3, 8))
        reporter.on_progress(2, 4, "n26", date(2024, 3, 8))  # same day → no header
        reporter.on_progress(3, 4, "spk", date(2024, 3, 9))  # new day → header
        out = capsys.readouterr().out
        assert out.count("2024-03-08") == 1
        assert "2024-03-09" in out
        # bank headers are suppressed in chronological mode
        assert "processing transactions" not in out


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

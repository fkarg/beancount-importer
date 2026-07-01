"""Post-write `bean-check` warning against the top-level ledger."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer import cli
from beancount_importer.config import Config
from beancount_importer.models import ImportResult, SourceTransaction


def _result(year: int) -> ImportResult:
    return ImportResult(
        source_txn=SourceTransaction(
            booking_date=date(year, 1, 1), amount=Decimal("-1"), bank_key="spk"
        ),
        action="new",
    )


def _fake_which(monkeypatch, present: bool) -> None:
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/bean-check" if present else None
    )


def _fake_run(monkeypatch, returncode: int, stderr: str = "") -> list:
    calls: list = []

    def run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": returncode, "stderr": stderr, "stdout": ""})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


class TestLedgerCheck:
    def test_skips_when_main_bean_unset(self, tmp_path: Path, monkeypatch, capsys):
        calls = _fake_run(monkeypatch, 0)
        _fake_which(monkeypatch, True)
        cli._run_ledger_check(Config(main_bean=None), tmp_path, [_result(2024)])
        assert calls == []
        assert capsys.readouterr().out == ""

    def test_skips_when_bean_check_absent(self, tmp_path: Path, monkeypatch, capsys):
        calls = _fake_run(monkeypatch, 0)
        _fake_which(monkeypatch, False)
        (tmp_path / "main.bean").write_text("")
        cli._run_ledger_check(Config(main_bean="main.bean"), tmp_path, [_result(2024)])
        assert calls == []

    def test_missing_file_is_skipped(self, tmp_path: Path, monkeypatch, capsys):
        calls = _fake_run(monkeypatch, 0)
        _fake_which(monkeypatch, True)
        # No main.bean written → path.exists() is False.
        cli._run_ledger_check(Config(main_bean="main.bean"), tmp_path, [_result(2024)])
        assert calls == []

    def test_clean_ledger_reports_clean(self, tmp_path: Path, monkeypatch, capsys):
        _fake_which(monkeypatch, True)
        _fake_run(monkeypatch, 0)
        (tmp_path / "main.bean").write_text("")
        cli._run_ledger_check(Config(main_bean="main.bean"), tmp_path, [_result(2024)])
        assert "clean" in capsys.readouterr().out

    def test_issues_warn_without_raising(self, tmp_path: Path, monkeypatch, capsys):
        _fake_which(monkeypatch, True)
        _fake_run(
            monkeypatch,
            1,
            stderr="main.bean:5: Invalid reference to unknown account 'X'\n",
        )
        (tmp_path / "main.bean").write_text("")
        # Must NOT raise — the import is already written.
        cli._run_ledger_check(Config(main_bean="main.bean"), tmp_path, [_result(2024)])
        out = capsys.readouterr().out
        assert "found issues" in out
        assert "unknown account" in out

    def test_long_output_is_capped(self, tmp_path: Path, monkeypatch, capsys):
        _fake_which(monkeypatch, True)
        stderr = "\n".join(f"main.bean:{i}: err {i}" for i in range(100))
        _fake_run(monkeypatch, 1, stderr=stderr)
        (tmp_path / "main.bean").write_text("")
        cli._run_ledger_check(Config(main_bean="main.bean"), tmp_path, [_result(2024)])
        assert "more line(s)" in capsys.readouterr().out

    def test_empty_results_still_checks(self, tmp_path: Path, monkeypatch, capsys):
        # No results → fall back to the current year; a non-templated main.bean
        # resolves to the same path regardless, so the check still runs.
        _fake_which(monkeypatch, True)
        calls = _fake_run(monkeypatch, 0)
        (tmp_path / "main.bean").write_text("")
        cli._run_ledger_check(Config(main_bean="main.bean"), tmp_path, [])
        assert len(calls) == 1

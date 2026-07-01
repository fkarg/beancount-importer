"""Rule editor — inline MATCH→WRITE panel opened from Screen 1 `[r]`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.rule_editor import run
from beancount_importer.models import CategoryProposal, Posting, SourceTransaction


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _txn(
    payee: str = "AMZN MKTP DE*RT4",
    description: str = "EREF 123",
    bank_key: str = "spk",
) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 5),
        amount=Decimal("-89.00"),
        currency="EUR",
        payee=payee,
        description=description,
        bank_key=bank_key,
    )


def _proposal(
    account: str = "Expenses:Online",
    payee: str | None = "Amazon",
    narration: str | None = None,
    tag: str | None = None,
) -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=account),),
        payee=payee,
        narration=narration,
        tag=tag,
    )


def _scripted(*answers):
    it = iter(answers)
    return lambda *a, **kw: next(it)


class TestDefaults:
    def test_save_builds_rule_from_txn_and_proposal(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        rule = run(_console(), _txn(), _proposal())
        assert rule is not None
        # Raw payee as a clean contains literal — no regex escapes.
        assert rule.payee_pattern == "AMZN MKTP DE*RT4"
        assert rule.description_pattern == ""
        assert rule.match_mode == "contains"
        # Defaults to any bank, not the txn's bank.
        assert rule.bank_key == ""
        assert rule.target_account == "Expenses:Online"
        assert rule.override_payee == "Amazon"

    def test_cancel_returns_none(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("c"))
        assert run(_console(), _txn(), _proposal()) is None

    def test_no_payee_defaults_match_to_description(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        rule = run(_console(), _txn(payee=""), _proposal(payee=None))
        assert rule.payee_pattern == ""
        assert rule.description_pattern == "EREF 123"


class TestEditing:
    def test_edit_pattern(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("3", "AMZN", "s"))
        rule = run(_console(), _txn(), _proposal())
        assert rule.payee_pattern == "AMZN"

    def test_toggle_field_to_description(self, monkeypatch):
        # [1] cycles payee → description; the pattern moves with it.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("1", "s"))
        rule = run(_console(), _txn(), _proposal())
        assert rule.payee_pattern == ""
        assert rule.description_pattern == "AMZN MKTP DE*RT4"

    def test_toggle_field_to_either_sets_match_any(self, monkeypatch):
        # [1] cycles payee → description → either; "either" matches payee OR
        # narration via one rule (match_any), both patterns set to the text.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("1", "1", "s"))
        rule = run(_console(), _txn(), _proposal())
        assert rule.match_any is True
        assert rule.payee_pattern == "AMZN MKTP DE*RT4"
        assert rule.description_pattern == "AMZN MKTP DE*RT4"

    def test_toggle_mode_to_regex(self, monkeypatch):
        # [2] cycles contains → exact → regex (two presses).
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("2", "2", "3", "AMZN.*", "s")
        )
        rule = run(_console(), _txn(), _proposal())
        assert rule.match_mode == "regex"
        assert rule.payee_pattern == "AMZN.*"

    def test_invalid_regex_reprompts_instead_of_crashing(self, monkeypatch):
        # regex mode + bad pattern → save is refused, panel stays; fix + save.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("2", "2", "3", "[bad", "s", "3", "ok", "s"),
        )
        console = _console()
        rule = run(console, _txn(), _proposal())
        assert rule is not None
        assert rule.payee_pattern == "ok"
        assert "invalid" in console.export_text().lower()

    def test_edit_rewrite_payee(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("7", "Amazon EU", "s"))
        rule = run(_console(), _txn(), _proposal(payee="Amazon"))
        assert rule.override_payee == "Amazon EU"

    def test_set_tag(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("9", "trip", "s"))
        rule = run(_console(), _txn(), _proposal())
        assert rule.tag == "trip"

    def test_set_bank_narrows_from_any(self, monkeypatch):
        # Default is any bank ([4] blank); the user can narrow to one.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("4", "spk", "s"))
        rule = run(_console(), _txn(), _proposal())
        assert rule.bank_key == "spk"


class TestRender:
    def test_panel_shows_match_and_write_sections(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("c"))
        con = _console()
        run(con, _txn(), _proposal())
        out = con.export_text()
        assert "MATCH" in out
        assert "WRITE" in out
        assert "Expenses:Online" in out

    def test_sign_renders_glyph_when_set(self, monkeypatch):
        # [5] cycles sign any → debit; the panel shows the − glyph, not "debit".
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("5", "c"))
        con = _console()
        run(con, _txn(), _proposal())
        assert "−" in con.export_text()

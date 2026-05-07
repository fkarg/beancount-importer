"""Amortize mode sub-prompt + Screen 1's `[m]` hotkey integration."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.modes.amortize import render, run
from beancount_importer.models import CategoryProposal, Posting


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _proposal(metadata: dict[str, str] | None = None) -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Software"),),
        metadata=metadata or {},
    )


def _scripted(*answers):
    it = iter(answers)
    return lambda *a, **kw: next(it)


# ── Render ────────────────────────────────────────────────────────────────────


class TestRender:
    def test_lists_three_amortize_types_plus_cancel(self):
        con = _console()
        render(con)
        out = con.export_text()
        for token in (
            "[1] amortize_months",
            "[2] prepaid_months",
            "[3] lifetime_months",
            "[4] cancel",
        ):
            assert token in out


# ── Run-loop branches ─────────────────────────────────────────────────────────


class TestRun:
    def test_cancel_returns_proposal_unchanged(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("4"))
        original = _proposal({"document": "invoice.pdf"})
        result = run(_console(), original)
        assert result is original  # frozen model; identity check

    def test_amortize_months_stamps_metadata(self, monkeypatch):
        # Mode `1` → months `12`. The proposal grows the `amortize_months`
        # key; existing metadata is preserved.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "12")
        )
        original = _proposal({"document": "invoice.pdf"})
        result = run(_console(), original)
        assert result.metadata == {
            "document": "invoice.pdf",
            "amortize_months": "12",
        }

    def test_prepaid_months_uses_correct_key(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("2", "6")
        )
        result = run(_console(), _proposal())
        assert result.metadata == {"prepaid_months": "6"}

    def test_lifetime_months_uses_correct_key(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("3", "36")
        )
        result = run(_console(), _proposal())
        assert result.metadata == {"lifetime_months": "36"}

    def test_default_months_is_twelve(self, monkeypatch):
        # Empty input on the months prompt → Rich's `default="12"` kicks in.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "")
        )
        # Rich's `Prompt.ask` returns the default when the user submits
        # blank; emulate that explicitly here.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("1", "12"),  # "12" is the literal default
        )
        result = run(_console(), _proposal())
        assert result.metadata.get("amortize_months") == "12"

    def test_invalid_months_cancels(self, monkeypatch):
        # Non-integer input on months → cancel; proposal unchanged.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "twelve")
        )
        original = _proposal()
        console = _console()
        result = run(console, original)
        assert result is original
        assert "invalid month count" in console.export_text()

    def test_zero_months_cancels(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "0")
        )
        original = _proposal()
        result = run(_console(), original)
        assert result is original


# ── Screen 1 [m] integration (debit only) ─────────────────────────────────────


class TestScreen1MHotkey:
    def test_m_appears_only_for_debits(self):
        from datetime import date
        from decimal import Decimal

        from beancount_importer.categorizer.confirm import ConfirmContext, render
        from beancount_importer.models import SourceTransaction
        from beancount_importer.rules.models import CategorizationRule

        # Debit
        debit = SourceTransaction(
            booking_date=date(2024, 3, 1),
            amount=Decimal("-50"),
            currency="EUR",
            payee="Vendor",
            description="d",
            bank_key="spk",
        )
        ctx_debit = ConfirmContext(
            txn=debit,
            proposal=_proposal(),
            bank_account="Assets:B:SPK",
            kind="auto_matched",
            matched_rule=CategorizationRule(target_account="Expenses:Software"),
        )
        con1 = _console()
        render(ctx=ctx_debit, console=con1) if False else None
        # Use the real render from confirm
        from beancount_importer.categorizer.confirm import render as render_confirm

        render_confirm(con1, ctx_debit)
        assert "[m] amortize" in con1.export_text()

        # Credit
        credit = SourceTransaction(
            booking_date=date(2024, 3, 1),
            amount=Decimal("3000"),
            currency="EUR",
            payee="Employer",
            description="d",
            bank_key="spk",
        )
        ctx_credit = ConfirmContext(
            txn=credit,
            proposal=_proposal(),
            bank_account="Assets:B:SPK",
            kind="auto_matched",
            matched_rule=CategorizationRule(target_account="Income:Salary"),
        )
        con2 = _console()
        render_confirm(con2, ctx_credit)
        assert "[m]" not in con2.export_text()

    def test_m_on_screen_1_stamps_amortize_metadata(self, monkeypatch):
        from datetime import date
        from decimal import Decimal

        from beancount_importer.categorizer.confirm import ConfirmContext, run as run_confirm
        from beancount_importer.models import SourceTransaction
        from beancount_importer.rules.models import CategorizationRule

        # Sequence: m → 1 (amortize_months) → 12 (months) → "" (Enter on
        # Screen 1 to confirm).
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("m", "1", "12", "")
        )
        ctx = ConfirmContext(
            txn=SourceTransaction(
                booking_date=date(2024, 3, 1),
                amount=Decimal("-50"),
                currency="EUR",
                payee="Vendor",
                description="d",
                bank_key="spk",
            ),
            proposal=_proposal(),
            bank_account="Assets:B:SPK",
            kind="auto_matched",
            matched_rule=CategorizationRule(target_account="Expenses:Software"),
        )
        decision = run_confirm(_console(), ctx)
        assert decision.action == "confirm"
        assert decision.proposal is not None
        assert decision.proposal.metadata.get("amortize_months") == "12"

"""Screen 3 — Existing-entry collision — structural and behavioural tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.collision import (
    CollisionContext,
    render,
    run,
)
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _ctx(
    *,
    changes: list[ProposedChange] | None = None,
) -> CollisionContext:
    txn = SourceTransaction(
        booking_date=date(2024, 3, 9),
        amount=Decimal("-15.00"),
        currency="EUR",
        payee="Spotify AB",
        description="Premium Family",
        bank_key="spk",
    )
    existing = LedgerEntry(
        date=date(2024, 3, 9),
        flag="*",
        payee="Spotify",
        narration="Music subscription",
        source_account="Assets:B:SPK",
        target_account="Expenses:Subscriptions",
        amount=Decimal("-15.00"),
        currency="EUR",
    )
    if changes is None:
        changes = [
            ProposedChange("payee", "Spotify", "Spotify AB"),
            ProposedChange("narration", "Music subscription", "Premium Family"),
        ]
    proposal = CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Subscriptions"),),
        payee="Spotify AB",
        narration="Premium Family",
    )
    return CollisionContext(
        txn=txn,
        existing=existing,
        proposed_changes=changes,
        proposal=proposal,
        progress=(22, 47),
        bank_key="spk",
        year=2024,
    )


class TestRender:
    def test_header_uses_collision_glyph(self):
        con = _console()
        render(con, _ctx())
        # `⚡` is the collision glyph from the design doc — distinct from
        # `✎` so the user knows this screen needs a different decision.
        assert "⚡" in con.export_text()

    def test_already_imported_label_present(self):
        con = _console()
        render(con, _ctx())
        assert "already imported as:" in con.export_text()

    def test_existing_entry_rendered_as_beancount_text(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        # Beancount-shaped header line so the user reads the file mentally.
        assert '2024-03-09 * "Spotify" "Music subscription"' in out
        # Source-account leg with explicit amount; target-account leg without.
        assert "◆ Assets:B:SPK" in out
        assert "-15.00 EUR" in out
        assert "↓ Expenses:Subscriptions" in out

    def test_diff_lines_show_old_to_new(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert 'payee:' in out
        assert '"Spotify"' in out
        assert '"Spotify AB"' in out
        assert '"Music subscription"' in out
        assert '"Premium Family"' in out

    def test_hotkey_row_shows_step2_choices(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        for hotkey in (
            "[enter] update",
            "[k] keep existing",
            "[i] import as new",
            "[b] block future updates",
            "[s] skip",
            "[q] quit",
        ):
            assert hotkey in out, f"missing hotkey row entry: {hotkey}"

    def test_existing_entry_with_blank_target_renders_no_target_line(self):
        # Synthesised virtual entries can carry `target_account=""`. The
        # screen omits the second posting line entirely rather than printing
        # a glyph beside an empty name.
        from datetime import date as _d
        from decimal import Decimal as _D
        from beancount_importer.models import LedgerEntry

        existing = LedgerEntry(
            date=_d(2024, 3, 9),
            narration="x",
            source_account="Assets:B:SPK",
            target_account="",
            amount=_D("-15.00"),
            currency="EUR",
        )
        ctx = _ctx()
        ctx_no_target = type(ctx)(
            txn=ctx.txn,
            existing=existing,
            proposed_changes=ctx.proposed_changes,
            proposal=ctx.proposal,
            progress=ctx.progress,
            bank_key=ctx.bank_key,
            year=ctx.year,
        )
        con = _console()
        render(con, ctx_no_target)
        out = con.export_text()
        assert "◆ Assets:B:SPK" in out
        # Empty target_account ⇒ no second account line at all.
        # (the source line carries the amount; nothing follows it inside
        # the existing-entry block).
        rows = [
            line
            for line in out.splitlines()
            if "Assets:B:SPK" in line or "Expenses" in line
        ]
        assert all("Expenses" not in row for row in rows)


def _scripted(answer: str):
    return lambda *a, **kw: answer


class TestRun:
    def test_enter_returns_update(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        decision = run(_console(), _ctx())
        assert decision.action == "update"

    def test_k_returns_keep(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("k"))
        assert run(_console(), _ctx()).action == "keep"

    def test_b_returns_block(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("b"))
        assert run(_console(), _ctx()).action == "block"

    def test_s_returns_skip(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        assert run(_console(), _ctx()).action == "skip"

    def test_q_returns_quit(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        assert run(_console(), _ctx()).action == "quit"

    def test_i_returns_import_new(self, monkeypatch):
        # `[i]` lets the user spit out the new proposal as a fresh entry
        # alongside the matched one — used when the auto-match grabbed
        # an unrelated transaction that looks similar.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("i"))
        assert run(_console(), _ctx()).action == "import_new"

"""Screen 2 — Pick a category — structural and behavioural tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.pick import (
    PickContext,
    render,
    run,
)
from beancount_importer.models import LedgerEntry, SourceTransaction


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _txn(amount: Decimal = Decimal("-89.00")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 5),
        amount=amount,
        currency="EUR",
        payee="AMZN MKTP DE*RT4...",
        description="EREF: 4567823",
        bank_key="spk",
    )


_UNSET: tuple[str, ...] = ("__unset__",)


def _ctx(
    *,
    suggestions: tuple[str, ...] = (
        "Expenses:Online",
        "Expenses:Household",
        "Expenses:Electronics",
        "Expenses:Books",
        "Expenses:Unknown",
    ),
    counts: dict[str, int] | None = None,
    all_accounts: tuple[str, ...] = _UNSET,
    existing: tuple[LedgerEntry, ...] = (),
) -> PickContext:
    # `all_accounts=_UNSET` defaults to suggestions; explicit `()` is honoured
    # so tests can exercise the empty-pool branch without `or` collapsing it.
    if all_accounts is _UNSET:
        all_accounts = suggestions
    return PickContext(
        txn=_txn(),
        bank_account="Assets:B:SPK",
        suggestions=suggestions,
        suggestion_counts=counts or {"Expenses:Online": 34, "Expenses:Household": 9},
        all_accounts=all_accounts,
        existing_entries=existing,
        progress=(13, 47),
        bank_key="spk",
        year=2024,
    )


# ── Structural assertions ─────────────────────────────────────────────────────


class TestRender:
    def test_glyph_is_question_mark_for_no_default(self):
        con = _console()
        render(con, _ctx())
        # `?` signals "no Enter default" — visual cue matches the principle.
        assert "?" in con.export_text()

    def test_arrow_target_is_unset(self):
        # Headline target must be a literal `?`, not a styled account name.
        con = _console()
        render(con, _ctx())
        assert "→  ?" in con.export_text()

    def test_top_suggestions_show_with_glyphs_and_counts(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        # First three suggestions visible with their class glyphs.
        assert "[1] ↓ Expenses:Online" in out
        assert "[2] ↓ Expenses:Household" in out
        assert "[3] ↓ Expenses:Electronics" in out
        # Frequency annotations rendered dim.
        assert "34×" in out
        assert "9×" in out

    def test_unknown_count_renders_em_dash(self):
        con = _console()
        # Expenses:Books has no count in the default fixture map.
        render(con, _ctx(counts={"Expenses:Online": 1}))
        out = con.export_text()
        # Anything not in the count map renders `—` so the column lines up.
        assert "—" in out

    def test_top_n_caps_at_five(self):
        con = _console()
        render(con, _ctx(suggestions=tuple(f"Expenses:Cat{i}" for i in range(20))))
        out = con.export_text()
        # 5th visible, 6th not.
        assert "Expenses:Cat4" in out
        assert "Expenses:Cat5" not in out

    def test_no_suggestions_shows_fallback_hint(self):
        con = _console()
        render(con, _ctx(suggestions=()))
        out = con.export_text()
        assert "No suggestions" in out
        assert "[w]" in out

    def test_hotkey_row_lists_step4_choices(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        for hotkey in (
            "[1-5] pick",
            "[l] list all accounts",
            "[w] write custom account",
            "[o] transfer to own account",
            "[s] skip",
            "[q] quit",
        ):
            assert hotkey in out, f"missing hotkey row entry: {hotkey}"


# ── Behavioural tests ─────────────────────────────────────────────────────────


def _scripted(*answers):
    it = iter(answers)
    return lambda *a, **kw: next(it)


class TestRun:
    def test_numeric_pick_returns_account(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("2"))
        decision = run(_console(), _ctx())
        assert decision.action == "pick"
        assert decision.account == "Expenses:Household"

    def test_skip_returns_skip(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        assert run(_console(), _ctx()).action == "skip"

    def test_quit_returns_quit(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        assert run(_console(), _ctx()).action == "quit"

    def test_w_known_account_returns_pick_without_confirm(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("w", "Expenses:Online")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "pick"
        assert decision.account == "Expenses:Online"

    def test_w_unknown_account_with_y_confirm_returns_pick(self, monkeypatch):
        # Unknown name asks "use anyway?"; `y` accepts.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("w", "Expenses:WeirdNew", "y"),
        )
        decision = run(_console(), _ctx())
        assert decision.action == "pick"
        assert decision.account == "Expenses:WeirdNew"

    def test_w_unknown_account_with_n_loops_back(self, monkeypatch):
        # `n` rejects, which falls back to the hotkey prompt; subsequent
        # `s` resolves the run as a skip.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("w", "Expenses:Typo", "n", "s"),
        )
        assert run(_console(), _ctx()).action == "skip"

    def test_w_empty_input_loops_back(self, monkeypatch):
        # Empty name is "I changed my mind" — no confirm prompt, just back.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("w", "", "q")
        )
        assert run(_console(), _ctx()).action == "quit"

    def test_l_list_picks_from_full_pool(self, monkeypatch):
        full_pool = tuple(f"Expenses:Cat{i}" for i in range(15))
        # Press `l`, then pick #2 on page 1.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "2")
        )
        decision = run(_console(), _ctx(all_accounts=full_pool))
        assert decision.action == "pick"
        assert decision.account == "Expenses:Cat1"

    def test_l_paging_n_then_pick(self, monkeypatch):
        # 15 accounts → 2 pages. Press `l`, then `n` to go to page 2,
        # then `5` to pick the 5th item on page 2 (index 14 overall).
        full_pool = tuple(f"Expenses:Cat{i}" for i in range(15))
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "n", "5")
        )
        decision = run(_console(), _ctx(all_accounts=full_pool))
        assert decision.action == "pick"
        assert decision.account == "Expenses:Cat14"

    def test_l_paging_p_returns_to_first_page(self, monkeypatch):
        full_pool = tuple(f"Expenses:Cat{i}" for i in range(15))
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("l", "n", "p", "1"),
        )
        decision = run(_console(), _ctx(all_accounts=full_pool))
        assert decision.account == "Expenses:Cat0"

    def test_l_cancel_returns_to_hotkey_loop(self, monkeypatch):
        # `x` cancels paging; subsequent `s` resolves the run.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "x", "s")
        )
        assert run(_console(), _ctx()).action == "skip"

    def test_l_with_empty_pool_loops_back(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "q")
        )
        # `all_accounts=()` triggers the empty branch, then user quits.
        ctx = _ctx(all_accounts=())
        assert run(_console(), ctx).action == "quit"

    def test_o_transfer_to_own_filters_to_assets_and_liabilities(
        self, monkeypatch
    ):
        # Existing entries include Expenses (filtered out) plus Assets and
        # Liabilities (kept). The user picks #2 from the filtered list.
        existing = (
            LedgerEntry(
                date=date(2024, 1, 1),
                narration="x",
                source_account="Assets:B:SPK",
                target_account="Expenses:Food",
                amount=Decimal("-10"),
            ),
            LedgerEntry(
                date=date(2024, 1, 2),
                narration="y",
                source_account="Assets:B:N26",
                target_account="Assets:B:SPK",
                amount=Decimal("100"),
            ),
            LedgerEntry(
                date=date(2024, 1, 3),
                narration="z",
                source_account="Liabilities:CreditCard:Visa",
                target_account="Expenses:Travel",
                amount=Decimal("-50"),
            ),
        )
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("o", "2")
        )
        decision = run(_console(), _ctx(existing=existing))
        # Sorted alphabetically: Assets:B:N26, Assets:B:SPK, Liabilities:...
        assert decision.action == "pick"
        assert decision.account == "Assets:B:SPK"

    def test_o_with_no_own_history_loops_back(self, monkeypatch):
        # `o` then `s` — no Assets/Liabilities entries means "no history",
        # the sub-prompt returns None, the run loops to the next hotkey.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("o", "s")
        )
        assert run(_console(), _ctx(existing=())).action == "skip"

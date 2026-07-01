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
from beancount_importer.models import SourceTransaction


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

    def test_top_n_caps_at_eight(self):
        con = _console()
        render(con, _ctx(suggestions=tuple(f"Expenses:Cat{i}" for i in range(20))))
        out = con.export_text()
        # 8th visible, 9th not.
        assert "Expenses:Cat7" in out
        assert "Expenses:Cat8" not in out

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
            "[1-8] pick",
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

    def test_l_pick_high_index_uses_multi_digit_input(self, monkeypatch):
        # 15 accounts in a single column grid. The accounts get sorted
        # alphabetically; "Expenses:Cat14" sorts BEFORE "Expenses:Cat2"
        # lexicographically — so "Cat14" is at index 6 in the sorted list.
        full_pool = tuple(f"Expenses:Cat{i}" for i in range(15))
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "6")
        )
        decision = run(_console(), _ctx(all_accounts=full_pool))
        assert decision.action == "pick"
        assert decision.account == sorted(full_pool)[5]

    def test_l_invalid_input_warns_and_reprompts(self, monkeypatch):
        # Non-numeric input shows a warning and re-prompts; subsequent
        # valid pick resolves.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "abc", "1")
        )
        console = _console()
        decision = run(console, _ctx())
        assert decision.action == "pick"
        assert "not a number" in console.export_text()

    def test_l_index_out_of_range_warns_and_reprompts(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "999", "1")
        )
        console = _console()
        decision = run(console, _ctx())
        assert decision.action == "pick"
        assert "out of range" in console.export_text()

    def test_l_enter_redraws_then_user_picks(self, monkeypatch):
        # Empty input on the full list redraws (no-op) and re-prompts.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "", "1")
        )
        decision = run(_console(), _ctx())
        assert decision.action == "pick"

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
        # `[o]` sources from the full chart (`all_accounts`), filtered to
        # Assets/Liabilities. Expenses are dropped; the user picks #2.
        all_accounts = (
            "Assets:B:N26",
            "Assets:B:SPK",
            "Expenses:Food",  # filtered out
            "Liabilities:CreditCard:Visa",
        )
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("o", "2")
        )
        decision = run(_console(), _ctx(all_accounts=all_accounts))
        # Sorted alphabetically: Assets:B:N26, Assets:B:SPK, Liabilities:...
        assert decision.action == "pick"
        assert decision.account == "Assets:B:SPK"

    def test_o_includes_opened_but_unused_liability(self, monkeypatch):
        # The bug that started this: a liability that only ever appears as a
        # 3rd posting leg (or is merely opened) was invisible in `[o]`. Sourced
        # from the chart it now lists, even with no transaction of its own.
        all_accounts = (
            "Assets:B:SPK",
            "Liabilities:CreditCard:Visa",
            "Liabilities:Familie:Anna",  # opened, never a captured posting
        )
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("o", "3")
        )
        decision = run(_console(), _ctx(all_accounts=all_accounts))
        assert decision.action == "pick"
        assert decision.account == "Liabilities:Familie:Anna"

    def test_o_with_no_own_history_loops_back(self, monkeypatch):
        # `o` then `s` — no Assets/Liabilities in the chart means "no history",
        # the sub-prompt returns None, the run loops to the next hotkey.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("o", "s")
        )
        assert run(_console(), _ctx(all_accounts=("Expenses:Food",))).action == "skip"


# ── Listing UX regressions ────────────────────────────────────────────────────


class TestFullListLayout:
    """The single-page column grid replaces the old 10-per-page pager.
    The on-call bug that drove the redesign: a user pressed Enter
    expecting "next page" and got silently cancelled back to Screen 2.
    No paging means no Enter ambiguity to begin with.
    """

    def test_accounts_are_sorted_alphabetically(self):
        # Listing must be alphabetical so the user can scan visually
        # for the account they want before typing the index.
        full_pool = ("Expenses:Zzz", "Expenses:Aaa", "Expenses:Mmm")
        monkeypatch_pool = sorted(full_pool)

        # Render path through the [l] entry: the order shown matches
        # `sorted(full_pool)`. Pick #1 — should be Expenses:Aaa.
        import pytest

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "rich.prompt.Prompt.ask", _scripted("l", "1")
            )
            decision = run(_console(), _ctx(all_accounts=full_pool))
        assert decision.account == monkeypatch_pool[0]

    def test_explicit_x_still_cancels(self, monkeypatch):
        # Sanity: cancel from the column grid still drops back to Screen 2.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "x", "s")
        )
        assert run(_console(), _ctx()).action == "skip"


class TestRedrawAfterCancel:
    def test_screen2_redraws_after_pager_cancel(self, monkeypatch):
        # After cancelling the pager with `x`, the user should see the
        # Screen 2 hotkey row again (so they know what their options
        # are). Tests for the visible re-render of "list all accounts".
        full_pool = tuple(f"Expenses:Cat{i}" for i in range(15))
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("l", "x", "s")
        )
        console = _console()
        run(console, _ctx(all_accounts=full_pool))
        # The hotkey row appears at least twice — once before the pager,
        # once after returning to Screen 2.
        assert console.export_text().count("[l] list all accounts") >= 2

    def test_screen2_redraws_after_w_empty_input(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("w", "", "s")
        )
        console = _console()
        run(console, _ctx())
        assert console.export_text().count("[l] list all accounts") >= 2

    def test_screen2_redraws_after_o_with_no_history(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("o", "s")
        )
        console = _console()
        run(console, _ctx(all_accounts=("Expenses:Food",)))
        # After the `[o]` sub-prompt prints "(no own-account history yet)"
        # and returns, Screen 2 should redraw before the next prompt.
        assert console.export_text().count("[l] list all accounts") >= 2


class TestFullListCounts:
    """`[l]` annotates each account with its usage count, mirroring the top
    suggestions — so a user scanning the full chart still sees which accounts
    they lean on. Opened-but-unused accounts show `—`.
    """

    def test_l_list_shows_usage_counts(self, monkeypatch):
        # `Expenses:Rare` is only in the full pool (not a suggestion), so its
        # `7×` can only come from the `[l]` grid, not the suggestions block.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("l", "x", "s"))
        console = _console()
        run(
            console,
            _ctx(
                all_accounts=("Expenses:Online", "Expenses:Rare"),
                counts={"Expenses:Rare": 7},
            ),
        )
        out = console.export_text()
        assert "7×" in out
        # `Expenses:Online` has no count → renders the em-dash placeholder.
        assert "—" in out

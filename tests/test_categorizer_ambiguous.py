"""Screen 4 — Ambiguous match — structural and behavioural tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.ambiguous import (
    AmbiguousContext,
    _bars,
    render,
    run,
)
from beancount_importer.models import LedgerEntry, SourceTransaction


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _entry(d: date, payee: str, amount: Decimal = Decimal("-34.50")) -> LedgerEntry:
    return LedgerEntry(
        date=d,
        narration="",
        payee=payee,
        source_account="Assets:B:PayPal",
        target_account="Expenses:Online",
        amount=amount,
        currency="EUR",
    )


def _ctx(
    *,
    candidates=None,
) -> AmbiguousContext:
    if candidates is None:
        candidates = (
            (_entry(date(2024, 3, 12), "eBay"), 1.5),
            (_entry(date(2024, 3, 10), "PayPal *eBay"), 1.0),
            (_entry(date(2024, 3, 15), "eBay"), 0.5),
        )
    return AmbiguousContext(
        txn=SourceTransaction(
            booking_date=date(2024, 3, 12),
            amount=Decimal("-34.50"),
            currency="EUR",
            payee="PayPal *eBay GmbH",
            bank_key="paypal",
        ),
        candidates=candidates,
        progress=(31, 47),
        bank_key="paypal",
        year=2024,
    )


# ── Bar rendering ─────────────────────────────────────────────────────────────


class TestBars:
    def test_full_bar_at_or_above_threshold(self):
        # The full bar saturates the visualisation; nobody reads "exactly
        # 2.0" — they read "very confident".
        assert _bars(2.0) == "▰▰▰▰▰"
        assert _bars(2.5) == "▰▰▰▰▰"

    def test_zero_or_negative_is_empty(self):
        assert _bars(0.0) == "▱▱▱▱▱"
        assert _bars(-1.0) == "▱▱▱▱▱"

    def test_one_dot_oh_renders_three_bars(self):
        # Round-half-up (not banker's) keeps the mid-score visually "medium".
        assert _bars(1.0) == "▰▰▰▱▱"

    def test_low_score_shows_one_bar(self):
        assert _bars(0.3) == "▰▱▱▱▱"

    def test_bar_string_is_always_five_segments(self):
        for score in (0.0, 0.25, 0.5, 1.0, 1.25, 1.5, 1.99, 2.0, 3.0):
            assert len(_bars(score)) == 5


# ── Structural assertions ─────────────────────────────────────────────────────


class TestRender:
    def test_glyph_is_question_mark(self):
        con = _console()
        render(con, _ctx())
        assert "?" in con.export_text()

    def test_headline_shows_amount_and_payee(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        assert "-34.50 EUR" in out
        assert "PayPal *eBay GmbH" in out

    def test_falls_back_to_description_when_no_payee(self):
        # Headline uses payee first, description second.
        con = _console()
        ctx = _ctx().__class__(
            txn=SourceTransaction(
                booking_date=date(2024, 3, 12),
                amount=Decimal("-34.50"),
                currency="EUR",
                description="Card payment",
                bank_key="paypal",
            ),
            candidates=_ctx().candidates,
            progress=(31, 47),
            bank_key="paypal",
            year=2024,
        )
        render(con, ctx)
        assert "Card payment" in con.export_text()

    def test_each_candidate_renders_with_score_bar(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        # Numbered hotkeys for each candidate.
        assert "[1]" in out
        assert "[2]" in out
        assert "[3]" in out
        # Bar visualisation present (not raw float scores).
        assert "▰" in out
        assert "▱" in out
        assert "1.5" not in out
        assert "1.0" not in out

    def test_candidate_target_account_styled(self):
        con = _console()
        render(con, _ctx())
        # Glyph + class on each candidate's target.
        assert "↓ Expenses:Online" in con.export_text()

    def test_candidate_with_blank_target_shows_question_mark(self):
        bare = (_entry(date(2024, 3, 12), "x"), 1.0)
        # `model_copy` to clear the target without going through the
        # normal constructor path.
        bare_entry = bare[0].model_copy(update={"target_account": ""})
        con = _console()
        render(con, _ctx(candidates=((bare_entry, 1.0),)))
        out = con.export_text()
        # The candidate row ends with `?` instead of a styled account.
        # We assert a row with `[1]` exists and contains a `?`.
        rows = [line for line in out.splitlines() if "[1]" in line]
        assert rows
        assert "?" in rows[0]

    def test_caps_visible_candidates_at_nine(self):
        # Beyond 9, the screen would need multi-digit hotkeys (forbidden
        # by the design doc). Extras are silently truncated.
        many = tuple(
            (_entry(date(2024, 3, i + 1), f"p{i}"), 0.5)
            for i in range(15)
        )
        con = _console()
        render(con, _ctx(candidates=many))
        out = con.export_text()
        assert "[9]" in out
        assert "[10]" not in out

    def test_hotkey_row_lists_step5_choices(self):
        con = _console()
        render(con, _ctx())
        out = con.export_text()
        for hk in (
            "[enter] pick #1",
            "[1-N] pick by number",
            "[i] import as new entry",
            "[s] skip",
            "[q] quit",
        ):
            assert hk in out, f"missing hotkey row entry: {hk}"


# ── Behavioural tests ─────────────────────────────────────────────────────────


def _scripted(answer: str):
    return lambda *a, **kw: answer


class TestRun:
    def test_enter_picks_top_candidate(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        decision = run(_console(), _ctx())
        assert decision.action == "pick"
        assert decision.entry is not None
        assert decision.entry.date == date(2024, 3, 12)

    def test_one_picks_top_candidate_same_as_enter(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("1"))
        decision = run(_console(), _ctx())
        assert decision.action == "pick"
        assert decision.entry is not None
        assert decision.entry.date == date(2024, 3, 12)

    def test_two_picks_second_candidate(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("2"))
        decision = run(_console(), _ctx())
        assert decision.action == "pick"
        assert decision.entry is not None
        assert decision.entry.date == date(2024, 3, 10)

    def test_i_returns_import_new(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("i"))
        decision = run(_console(), _ctx())
        assert decision.action == "import_new"
        assert decision.entry is None

    def test_s_returns_skip(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        assert run(_console(), _ctx()).action == "skip"

    def test_q_returns_quit(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        assert run(_console(), _ctx()).action == "quit"

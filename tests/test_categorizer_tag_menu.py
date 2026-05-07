"""Tag menu sub-prompt — Screen 1's `[t]` hotkey target."""

from __future__ import annotations

from datetime import date
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.tag_menu import render, run
from beancount_importer.rules.tags import ActiveTag


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _scripted(*answers):
    it = iter(answers)
    return lambda *a, **kw: next(it)


# ── Render ────────────────────────────────────────────────────────────────────


class TestRender:
    def test_no_active_tag_message(self):
        con = _console()
        render(con, current=None)
        assert "no active tag" in con.export_text()

    def test_active_tag_message_includes_mode(self):
        con = _console()
        render(con, current=ActiveTag(tag="trip", mode="always"))
        out = con.export_text()
        assert "active: trip" in out
        assert "mode=always" in out

    def test_lists_all_five_options(self):
        con = _console()
        render(con, current=None)
        out = con.export_text()
        # Numbered hotkeys for each mode.
        for token in (
            "[1] set once",
            "[2] set always",
            "[3] set until",
            "[4] clear",
            "[5] cancel",
        ):
            assert token in out


# ── Run-loop branches ─────────────────────────────────────────────────────────


class TestRunBranches:
    def test_cancel_returns_none(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("5"))
        assert run(_console(), current=None) is None

    def test_clear_returns_clear_delta(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("4"))
        delta = run(_console(), current=ActiveTag(tag="x", mode="always"))
        assert delta is not None
        assert delta.op == "clear"
        assert delta.new_state is None

    def test_once_sets_active_with_mode_once(self, monkeypatch):
        # Mode hotkey, then tag name.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("1", "trip")
        )
        delta = run(_console(), current=None)
        assert delta is not None
        assert delta.op == "set"
        assert delta.new_state is not None
        assert delta.new_state.tag == "trip"
        assert delta.new_state.mode == "once"

    def test_always_sets_active_with_mode_always(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("2", "trip")
        )
        delta = run(_console(), current=None)
        assert delta is not None
        assert delta.new_state is not None
        assert delta.new_state.mode == "always"

    def test_until_sets_duration_with_until_date(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("3", "trip", "2024-09-15"),
        )
        delta = run(_console(), current=None)
        assert delta is not None
        assert delta.new_state is not None
        assert delta.new_state.mode == "duration"
        assert delta.new_state.until_date == date(2024, 9, 15)

    def test_empty_tag_name_cancels(self, monkeypatch):
        # Picked "always" then submitted an empty tag name → no change.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("2", "")
        )
        assert run(_console(), current=None) is None

    def test_invalid_until_date_cancels_with_warning(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("3", "trip", "not-a-date"),
        )
        console = _console()
        assert run(console, current=None) is None
        assert "invalid date" in console.export_text()

    def test_empty_until_date_cancels(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("3", "trip", ""),
        )
        assert run(_console(), current=None) is None

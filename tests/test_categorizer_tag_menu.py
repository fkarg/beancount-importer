"""Tag menu sub-prompt — Screen 1's `[t]` hotkey target."""

from __future__ import annotations

from datetime import date
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.tag_menu import render, run
from beancount_importer.rules.tags import ActiveTag, RememberedTag


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


# Sentinel: the scripted answer "accept the prompt's pre-filled default",
# mirroring Rich returning `default` on empty input (which a plain stub can't).
_USE_DEFAULT = object()


def _scripted(*answers):
    it = iter(answers)

    def ask(*a, **kw):
        ans = next(it)
        return kw.get("default", "") if ans is _USE_DEFAULT else ans

    return ask


_KNOWN = (
    RememberedTag(tag="usa-2024", until_date=date(2024, 4, 11)),
    RememberedTag(tag="italy"),
)


# ── Render ────────────────────────────────────────────────────────────────────


class TestRender:
    def test_no_active_tag_shows_pick_prompt(self):
        con = _console()
        render(con, current=None)
        assert "pick a tag" in con.export_text()

    def test_active_tag_message_includes_mode(self):
        con = _console()
        render(con, current=ActiveTag(tag="trip", mode="always"))
        out = con.export_text()
        assert "active: trip" in out
        assert "mode=always" in out

    def test_lists_step1_controls(self):
        con = _console()
        render(con, current=None)
        out = con.export_text()
        # Step 1 is name-selection: new / clear / cancel, no mode numbers.
        for token in ("[n] new tag", "[c] clear", "[.] cancel"):
            assert token in out
        assert "set once" not in out


# ── Run-loop branches ─────────────────────────────────────────────────────────


class TestRunBranches:
    def test_cancel_returns_none(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("."))
        assert run(_console(), current=None) is None

    def test_clear_returns_clear_delta(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("c"))
        delta = run(_console(), current=ActiveTag(tag="x", mode="always"))
        assert delta is not None
        assert delta.op == "clear"
        assert delta.new_state is None

    def test_once_sets_active_with_mode_once(self, monkeypatch):
        # New tag: [n] → name → mode.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("n", "trip", "1")
        )
        delta = run(_console(), current=None)
        assert delta is not None
        assert delta.op == "set"
        assert delta.new_state is not None
        assert delta.new_state.tag == "trip"
        assert delta.new_state.mode == "once"

    def test_always_sets_active_with_mode_always(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("n", "trip", "2")
        )
        delta = run(_console(), current=None)
        assert delta is not None
        assert delta.new_state is not None
        assert delta.new_state.mode == "always"

    def test_until_sets_duration_with_until_date(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "trip", "3", "2024-09-15"),
        )
        delta = run(_console(), current=None)
        assert delta is not None
        assert delta.new_state is not None
        assert delta.new_state.mode == "duration"
        assert delta.new_state.until_date == date(2024, 9, 15)

    def test_empty_tag_name_cancels(self, monkeypatch):
        # [n] new tag, then an empty name → no change (mode never asked).
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("n", "")
        )
        assert run(_console(), current=None) is None

    def test_invalid_until_date_cancels_with_warning(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "trip", "3", "not-a-date"),
        )
        console = _console()
        assert run(console, current=None) is None
        assert "invalid date" in console.export_text()

    def test_empty_until_date_cancels(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            _scripted("n", "trip", "3", ""),
        )
        assert run(_console(), current=None) is None


# ── Known-tag picker ──────────────────────────────────────────────────────────


class TestPicker:
    def test_render_lists_known_tags(self):
        con = _console()
        render(con, None, _KNOWN)
        out = con.export_text()
        assert "[a] usa-2024" in out
        assert "[b] italy" in out

    def test_render_omits_picker_when_empty(self):
        con = _console()
        render(con, None)
        # No known-tag chips when the picker is empty.
        assert "[a]" not in con.export_text()

    def test_pick_known_then_always(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("a", "2"))
        delta = run(_console(), None, _KNOWN)
        assert delta is not None and delta.new_state is not None
        assert delta.new_state.tag == "usa-2024"
        assert delta.new_state.mode == "always"

    def test_pick_known_until_prefills_remembered_window(self, monkeypatch):
        # letter a → mode 3 (until) → accept the pre-filled remembered date.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("a", "3", _USE_DEFAULT)
        )
        delta = run(_console(), None, _KNOWN)
        assert delta is not None and delta.new_state is not None
        assert delta.new_state.mode == "duration"
        assert delta.new_state.until_date == date(2024, 4, 11)

    def test_pick_known_until_override_date(self, monkeypatch):
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("a", "3", "2025-01-01")
        )
        delta = run(_console(), None, _KNOWN)
        assert delta is not None and delta.new_state is not None
        assert delta.new_state.until_date == date(2025, 1, 1)

    def test_pick_name_only_tag_asks_mode(self, monkeypatch):
        # `italy` has no remembered window; pick b → once.
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("b", "1"))
        delta = run(_console(), None, _KNOWN)
        assert delta is not None and delta.new_state is not None
        assert delta.new_state.tag == "italy"
        assert delta.new_state.mode == "once"

    def test_pick_name_only_until_has_no_default_so_empty_cancels(self, monkeypatch):
        # `italy` has no window → the date prompt's default is empty →
        # accepting it cancels (no bad date committed).
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", _scripted("b", "3", _USE_DEFAULT)
        )
        assert run(_console(), None, _KNOWN) is None

"""Shared screen primitives — `hotkey`, `styled_account`, `bottom_rule`."""

from __future__ import annotations

import os
import pty
import sys
import termios
import tty
from contextlib import contextmanager
from datetime import date
from io import StringIO

import pytest
from rich.console import Console

from beancount_importer.categorizer.screen import (
    RULE,
    RULE_WIDTH,
    _classify_key,
    ask_hotkey,
    bottom_rule,
    hotkey,
    styled_account,
    tag_remaining_days,
)
from beancount_importer.rules.tags import ActiveTag


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


class TestHotkey:
    def test_renders_bracketed_letter_in_cyan(self):
        con = _console()
        con.print(f"start {hotkey('n')} end")
        # Brackets must appear literally — not interpreted as Rich tags.
        assert "[n]" in con.export_text()

    def test_supports_multi_character_labels(self):
        # `enter` is a multi-character "key"; the markup must escape both
        # ends of the bracketed token regardless of length.
        con = _console()
        con.print(hotkey("enter"))
        assert "[enter]" in con.export_text()


class TestStyledAccount:
    def test_known_class_includes_glyph(self):
        con = _console()
        con.print(styled_account("Expenses:Food"))
        out = con.export_text()
        assert "↓ Expenses:Food" in out

    def test_unknown_class_uses_neutral_glyph(self):
        con = _console()
        con.print(styled_account("Equity:Opening"))
        out = con.export_text()
        # Equity is in the table; renders ⊕. Not a literal "·".
        assert "⊕ Equity:Opening" in out

    def test_truly_unknown_prefix_falls_back_to_neutral(self):
        con = _console()
        con.print(styled_account("Custom:Whatever"))
        assert "· Custom:Whatever" in con.export_text()


class TestBottomRule:
    def test_writes_full_width_line(self):
        con = _console()
        bottom_rule(con)
        out = con.export_text()
        assert RULE in out
        assert len(RULE) == RULE_WIDTH


class TestClassifyKey:
    VALID = frozenset({"", "1", "s"})

    def test_ctrl_l_is_redraw(self):
        assert _classify_key(b"\x0c", self.VALID) == ("redraw", "")

    def test_enter_maps_to_empty_key(self):
        # CR and LF both mean "Enter", which the design treats as a hotkey.
        assert _classify_key(b"\r", self.VALID) == ("key", "")
        assert _classify_key(b"\n", self.VALID) == ("key", "")

    def test_valid_char_returns_key(self):
        assert _classify_key(b"1", self.VALID) == ("key", "1")

    def test_unknown_char_is_ignored(self):
        # A key not in `choices` is swallowed, mirroring `Prompt.ask`'s
        # choice-list rejection.
        assert _classify_key(b"z", self.VALID) == ("ignore", "")

    def test_eof_and_ctrl_d_raise(self):
        for byte in (b"", b"\x04"):
            with pytest.raises(EOFError):
                _classify_key(byte, self.VALID)


@contextmanager
def _stdin_feeding(keystrokes: bytes):
    """Stand `sys.stdin` up as a pty slave preloaded with `keystrokes`.

    The slave is put in cbreak (non-canonical, no echo) *before* the bytes
    are written, so `_read_hotkey`'s `os.read` sees them immediately —
    bytes queued in canonical mode wouldn't be readable without a newline,
    and echo on the slave would back up `TCSADRAIN` on restore.
    """
    master, slave = pty.openpty()
    tty.setcbreak(slave, termios.TCSANOW)
    os.write(master, keystrokes)
    stdin = os.fdopen(slave, "rb", buffering=0)
    saved = sys.stdin
    sys.stdin = stdin
    try:
        yield
    finally:
        sys.stdin = saved
        stdin.close()
        os.close(master)


class TestAskHotkeyRawTty:
    """The interactive single-key path: only taken on a real terminal,
    so the stream is a pty slave whose `isatty()` is True.
    """

    def test_ctrl_l_clears_redraws_then_returns_next_key(self):
        console = Console(file=StringIO(), force_terminal=True)
        redraws: list[bool] = []
        # Ctrl-L → redraw; 'z' → not a choice, ignored; '1' → accepted.
        with _stdin_feeding(b"\x0cz1"):
            result = ask_hotkey(
                ("", "1", "s"),
                console=console,
                redraw=lambda: redraws.append(True),
            )
        assert result == "1"
        assert redraws == [True]

    def test_enter_returns_empty_key(self):
        console = Console(file=StringIO(), force_terminal=True)
        with _stdin_feeding(b"\r"):
            result = ask_hotkey(
                ("", "1"), console=console, redraw=lambda: None
            )
        assert result == ""


class TestAskHotkeyFallback:
    def test_uses_prompt_ask_without_console(self, monkeypatch):
        # No console/redraw → line-buffered path, regardless of tty state.
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", lambda *a, **k: "  s  "
        )
        assert ask_hotkey(("", "s")) == "s"

    def test_uses_prompt_ask_when_not_a_tty(self, monkeypatch):
        # console+redraw supplied, but under pytest stdin isn't a terminal
        # (`isatty()` is False) → still the line-buffered path.
        assert not sys.stdin.isatty()
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "q")
        console = Console(file=StringIO())
        assert (
            ask_hotkey(("", "q"), console=console, redraw=lambda: None) == "q"
        )


class TestTagRemainingDays:
    def test_none_for_no_tag(self):
        assert tag_remaining_days(None, date(2024, 3, 4)) is None

    def test_none_for_non_duration_mode(self):
        tag = ActiveTag(tag="trip", mode="always")
        assert tag_remaining_days(tag, date(2024, 3, 4)) is None

    def test_none_for_duration_without_until(self):
        tag = ActiveTag(tag="trip", mode="duration")
        assert tag_remaining_days(tag, date(2024, 3, 4)) is None

    def test_counts_inclusive_days_to_until_date(self):
        tag = ActiveTag(tag="trip", mode="duration", until_date=date(2024, 3, 8))
        assert tag_remaining_days(tag, date(2024, 3, 4)) == 4

    def test_clamps_expired_tag_to_zero(self):
        tag = ActiveTag(tag="trip", mode="duration", until_date=date(2024, 3, 1))
        assert tag_remaining_days(tag, date(2024, 3, 4)) == 0

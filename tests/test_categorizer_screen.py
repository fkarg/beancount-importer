"""Shared screen primitives — `hotkey`, `styled_account`, `bottom_rule`."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.screen import (
    RULE,
    RULE_WIDTH,
    bottom_rule,
    hotkey,
    styled_account,
)


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

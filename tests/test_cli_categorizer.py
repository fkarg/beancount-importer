"""Unit tests for the interactive categorizer's pure helpers."""

from __future__ import annotations

from beancount_importer.cli import (
    _render_account_suggestions,
    _resolve_account_pick,
)


class TestResolveAccountPick:
    def test_numeric_in_range_picks_from_hints(self):
        hints = ("Expenses:Food", "Expenses:Rent", "Income:Salary")
        assert _resolve_account_pick("2", hints) == "Expenses:Rent"

    def test_numeric_out_of_range_returns_literal(self):
        # `2024` could be a year mistakenly typed; do not silently pick
        # suggestion #2024 (which doesn't exist anyway).
        hints = ("Expenses:Food", "Expenses:Rent")
        assert _resolve_account_pick("2024", hints) == "2024"

    def test_non_numeric_returns_literal_account(self):
        hints = ("Expenses:Food",)
        assert _resolve_account_pick("Expenses:Custom", hints) == "Expenses:Custom"

    def test_whitespace_is_stripped(self):
        hints = ("Expenses:Food", "Expenses:Rent")
        assert _resolve_account_pick("  1 ", hints) == "Expenses:Food"

    def test_empty_hints_falls_through(self):
        assert _resolve_account_pick("1", ()) == "1"


class TestRenderAccountSuggestions:
    def test_empty_hints_returns_none(self):
        assert _render_account_suggestions(()) is None

    def test_renders_numbered_table(self):
        table = _render_account_suggestions(("Expenses:Food", "Expenses:Rent"))
        assert table is not None
        # Two columns (#, account) and two rows
        assert len(table.columns) == 2
        assert table.row_count == 2

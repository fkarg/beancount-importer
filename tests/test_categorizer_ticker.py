"""Per-result ticker formatting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.ticker import format_line
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule


def _txn(amount: Decimal = Decimal("-12.50"), payee: str = "Starbucks") -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 3, 4),
        amount=amount,
        currency="EUR",
        payee=payee,
        description="d",
        bank_key="spk",
    )


def _proposal(target: str = "Expenses:Food", payee: str = "Starbucks") -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=target),),
        payee=payee,
    )


def _rendered(result: ImportResult) -> str:
    """Run `format_line` through a Rich Console so style tags resolve to text."""
    con = Console(file=StringIO(), record=True, width=120, emoji=False)
    con.print(format_line(result))
    return con.export_text().rstrip()


class TestClassification:
    def test_new_with_rule_shows_rule_glyph(self):
        result = ImportResult(
            source_txn=_txn(),
            action="new",
            proposal=_proposal(),
            rule_matched=CategorizationRule(target_account="Expenses:Food"),
        )
        line = _rendered(result)
        assert line.startswith("✓")
        assert "(rule)" in line

    def test_new_without_rule_shows_user_glyph(self):
        result = ImportResult(
            source_txn=_txn(),
            action="new",
            proposal=_proposal(),
        )
        line = _rendered(result)
        assert line.startswith("✓")
        assert "(you)" in line

    def test_replay_overrides_other_classifications(self):
        # `is_replay` wins even if a rule was matched on the original run.
        result = ImportResult(
            source_txn=_txn(),
            action="new",
            proposal=_proposal(),
            rule_matched=CategorizationRule(target_account="Expenses:Food"),
            is_replay=True,
        )
        line = _rendered(result)
        assert line.startswith("↻")
        assert "(replay)" in line

    def test_dedup_skip_renders_as_matched(self):
        # `skip_reason="duplicate"` is the cheap-dedup mechanism, but
        # from the user's view it's the same outcome as the other
        # "already in your ledger" paths — surface a single word.
        result = ImportResult(
            source_txn=_txn(),
            action="skip",
            skip_reason="duplicate",
        )
        line = _rendered(result)
        assert line.startswith("…")
        assert "matched" in line
        assert "duplicate" not in line  # internal label not leaked

    def test_skip_rule_label(self):
        # Suppression via a `skip_update_patterns` rule — distinct
        # from "matched" because nothing was actually paired.
        result = ImportResult(
            source_txn=_txn(),
            action="skip",
            skip_reason="skip_rule",
        )
        assert "skipped (rule)" in _rendered(result)

    def test_cross_source_match_label(self):
        # Worth surfacing the mechanism here — the row's counterpart
        # lives on another bank, which is useful debugging context.
        result = ImportResult(
            source_txn=_txn(),
            action="skip",
            skip_reason="cross_source_match",
        )
        assert "matched (other bank)" in _rendered(result)

    def test_user_skipped_label(self):
        result = ImportResult(
            source_txn=_txn(),
            action="skip",
            skip_reason="user_skipped",
        )
        assert "skipped (you)" in _rendered(result)

    def test_user_blocked_label(self):
        result = ImportResult(
            source_txn=_txn(),
            action="skip",
            skip_reason="user_blocked",
        )
        assert "blocked" in _rendered(result)

    def test_skip_without_reason_falls_back_to_skipped(self):
        # Defensive: a `skip` action with no `skip_reason` set still
        # renders a ticker line rather than crashing — the fallback
        # surfaces the bare word "skipped" so the row stays visible.
        result = ImportResult(source_txn=_txn(), action="skip")
        line = _rendered(result)
        assert line.startswith("…")
        assert "skipped" in line

    def test_update_with_changes_shows_pencil(self):
        existing = LedgerEntry(
            date=date(2024, 3, 9),
            narration="x",
            source_account="Assets:B:SPK",
            target_account="Expenses:Subscriptions",
            amount=Decimal("-15.00"),
        )
        result = ImportResult(
            source_txn=_txn(),
            action="update",
            matched_entry=existing,
            proposed_changes=[ProposedChange("payee", "old", "new")],
            proposal=_proposal(target="Expenses:Subscriptions"),
        )
        line = _rendered(result)
        assert line.startswith("✎")
        assert "(updated)" in line

    def test_update_with_empty_changes_silent_skips(self):
        existing = LedgerEntry(
            date=date(2024, 3, 9),
            narration="x",
            source_account="Assets:B:SPK",
            target_account="Expenses:Subscriptions",
            amount=Decimal("-15.00"),
        )
        result = ImportResult(
            source_txn=_txn(),
            action="update",
            matched_entry=existing,
            proposed_changes=[],
            proposal=_proposal(target="Expenses:Subscriptions"),
        )
        line = _rendered(result)
        assert line.startswith("…")
        assert "matched" in line

    def test_user_kept_renders_with_confirmation_note(self):
        # Screen 3's `[k] keep` path: the user explicitly confirmed
        # the match — surface that so the scrollback doesn't look
        # identical to the silent-skip case.
        existing = LedgerEntry(
            date=date(2024, 3, 9),
            narration="x",
            source_account="Assets:B:SPK",
            target_account="Expenses:Subscriptions",
            amount=Decimal("-15.00"),
        )
        result = ImportResult(
            source_txn=_txn(),
            action="update",
            matched_entry=existing,
            proposed_changes=[],
            proposal=_proposal(target="Expenses:Subscriptions"),
            skip_reason="user_kept",
        )
        line = _rendered(result)
        assert "matched (you confirmed)" in line


class TestRendering:
    def test_amount_includes_sign_and_currency(self):
        result = ImportResult(
            source_txn=_txn(amount=Decimal("3000.00")),
            action="new",
            proposal=_proposal(),
        )
        assert "+3000.00 EUR" in _rendered(result)

    def test_target_account_styled_with_glyph(self):
        result = ImportResult(
            source_txn=_txn(),
            action="new",
            proposal=_proposal(target="Income:Salary"),
        )
        line = _rendered(result)
        # Income glyph + name follow the arrow.
        assert "→ ↑ Income:Salary" in line

    def test_skip_omits_target_chunk(self):
        result = ImportResult(
            source_txn=_txn(),
            action="skip",
            skip_reason="duplicate",
        )
        line = _rendered(result)
        # No arrow at all — the row isn't booking anything.
        assert "→" not in line

    def test_long_payee_is_truncated(self):
        long_payee = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        result = ImportResult(
            source_txn=_txn(payee=long_payee),
            action="new",
            proposal=_proposal(payee=long_payee),
        )
        line = _rendered(result)
        # Truncation marker present; original full payee is not.
        assert "…" in line
        assert long_payee not in line

    def test_quit_action_shows_warning_glyph(self):
        # A user-issued quit still produces a ticker line so the
        # scrollback shows where the run stopped.
        result = ImportResult(source_txn=_txn(), action="quit")
        line = _rendered(result)
        assert line.startswith("⚠")
        assert "(quit)" in line

    def test_unknown_action_falls_back_to_action_label(self):
        # Defensive: a future action variant (e.g. `transfer`) still
        # renders rather than crashing the run.
        result = ImportResult(source_txn=_txn(), action="transfer")
        line = _rendered(result)
        assert line.startswith("✓")
        assert "(transfer)" in line

    def test_proposal_payee_overrides_txn_payee(self):
        # When a rule rewrites payee, the ticker should show the rewritten
        # version (it's what was written), not the raw CSV value.
        result = ImportResult(
            source_txn=_txn(payee="STARBUCKS COFFEE GMBH"),
            action="new",
            proposal=_proposal(payee="Starbucks"),
            rule_matched=CategorizationRule(target_account="Expenses:Food"),
        )
        line = _rendered(result)
        assert "Starbucks" in line
        # Truncated form would show `STARBUCKS COFFEE …` if the txn
        # payee leaked through; assert the original isn't present.
        assert "STARBUCKS COFFEE" not in line

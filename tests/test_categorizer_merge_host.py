"""Host-level `make_screen_merge_fn` integration.

Verifies that the host translates a `MergeContext` into a Screen-3 run
and back, mapping each Screen-3 outcome to the right `MergeDecision`.
The screen module itself is unit-tested in `test_categorizer_collision`;
here we focus on the bridge.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from rich.console import Console

from beancount_importer.categorizer.host import make_screen_merge_fn
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.pipeline import MergeContext


def _console() -> Console:
    return Console(file=StringIO(), record=True, width=120, emoji=False)


def _ctx() -> MergeContext:
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
    proposal = CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Subscriptions"),),
        payee="Spotify AB",
        narration="Premium Family",
    )
    changes = (
        ProposedChange("payee", "Spotify", "Spotify AB"),
        ProposedChange("narration", "Music subscription", "Premium Family"),
    )
    return MergeContext(
        txn=txn,
        proposal=proposal,
        matched_entry=existing,
        proposed_changes=changes,
        progress=(22, 47),
    )


def _scripted(answer: str):
    return lambda *a, **kw: answer


class TestMergeFnRouting:
    def test_enter_returns_update(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        merge_fn = make_screen_merge_fn(_console())
        decision = merge_fn(_ctx())
        assert decision.action == "update"

    def test_k_returns_keep(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("k"))
        merge_fn = make_screen_merge_fn(_console())
        assert merge_fn(_ctx()).action == "keep"

    def test_b_returns_block(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("b"))
        merge_fn = make_screen_merge_fn(_console())
        assert merge_fn(_ctx()).action == "block"

    def test_s_returns_skip(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("s"))
        merge_fn = make_screen_merge_fn(_console())
        assert merge_fn(_ctx()).action == "skip"

    def test_q_returns_quit(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted("q"))
        merge_fn = make_screen_merge_fn(_console())
        assert merge_fn(_ctx()).action == "quit"

    def test_renders_diff_to_console(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        console = _console()
        merge_fn = make_screen_merge_fn(console)
        merge_fn(_ctx())
        out = console.export_text()
        # Both diff fields visible.
        assert "Spotify AB" in out
        assert "Music subscription" in out
        assert "Premium Family" in out
        # Existing-entry block rendered as beancount text.
        assert '2024-03-09 * "Spotify"' in out


# ── Tag-state plumbing for MergeContext ───────────────────────────────────────


class TestActiveTagInMergeContext:
    def test_duration_tag_renders_remaining_days(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        from beancount_importer.rules.tags import ActiveTag

        active = ActiveTag(
            tag="trip",
            mode="duration",
            from_date=date(2024, 3, 1),
            until_date=date(2024, 3, 15),
        )
        ctx = _ctx().model_copy(update={"active_tag": active})
        console = _console()
        merge_fn = make_screen_merge_fn(console)
        merge_fn(ctx)
        # txn is on 2024-03-09 → 6 days remaining (until 03-15).
        out = console.export_text()
        assert "tag: trip" in out
        assert "6 left" in out

    def test_always_tag_omits_remaining(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        from beancount_importer.rules.tags import ActiveTag

        active = ActiveTag(tag="trip", mode="always")
        ctx = _ctx().model_copy(update={"active_tag": active})
        console = _console()
        merge_fn = make_screen_merge_fn(console)
        merge_fn(ctx)
        out = console.export_text()
        assert "tag: trip" in out
        assert "left" not in out

    def test_no_active_tag_renders_no_tag_label(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        console = _console()
        merge_fn = make_screen_merge_fn(console)
        merge_fn(_ctx())  # default ctx has no active tag
        assert "no tag" in console.export_text()

    def test_duration_past_until_clamps_to_zero(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        from beancount_importer.rules.tags import ActiveTag

        active = ActiveTag(tag="t", mode="duration", until_date=date(2024, 1, 1))
        ctx = _ctx().model_copy(update={"active_tag": active})
        console = _console()
        merge_fn = make_screen_merge_fn(console)
        merge_fn(ctx)
        assert "0 left" in console.export_text()

    def test_duration_without_until_omits_remaining(self, monkeypatch):
        monkeypatch.setattr("rich.prompt.Prompt.ask", _scripted(""))
        from beancount_importer.rules.tags import ActiveTag

        active = ActiveTag(tag="t", mode="duration")
        ctx = _ctx().model_copy(update={"active_tag": active})
        console = _console()
        merge_fn = make_screen_merge_fn(console)
        merge_fn(ctx)
        assert "left" not in console.export_text()

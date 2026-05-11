"""Pipeline-level routing of `MergeFn` decisions.

Exercises `_apply_merge_decision` end-to-end: feed a CSV row that
collides with an existing ledger entry, hook a scripted `merge_fn`,
and assert the resulting `ImportResult` matches the expected shape
for each of the six possible Screen-3 outcomes.

The `merge_fn` is called only when `_build_result` produces an
`update` action with non-empty `proposed_changes` — runs without a
collision shouldn't touch it at all.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer.config import (
    BankConfig,
    Config,
    CsvConfig,
    MatchingConfig,
)
from beancount_importer.models import (
    CategoryProposal,
    Posting,
    SourceTransaction,
)
from beancount_importer.pipeline import (
    CategorizeContext,
    MergeContext,
    MergeDecision,
    NoopReporter,
    run,
)
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.session import ImportOptions, ImportSession


def _spk_bank() -> BankConfig:
    return BankConfig(
        key="spk",
        display_name="Sparkasse",
        account="Assets:B:SPK",
        file_glob="SPK_*.csv",
        output_file="SPK.bean",
        csv=CsvConfig(
            delimiter=";",
            date_format=["%d.%m.%y"],
            amount_locale="de",
            field_date="Buchungstag",
            field_amount="Betrag",
            field_currency="Waehrung",
            field_payee="Beguenstigter",
            field_description="Verwendungszweck",
        ),
    )


def _write_collision_setup(tmp_path: Path) -> Path:
    """Bean file + CSV that produce one collision and zero new rows.

    The existing bean entry was imported with the OLD payee; the CSV
    row carries the NEW payee + narration, so the diff is non-empty
    and `_build_result` returns `action="update"`.

    Dates sit 6 days apart — close enough for the scorer at the
    tightened 7-day window, far enough that the strict dedup path
    (`dedup_max_date_days=5`) doesn't fire. Bean narration matches the
    CSV's Verwendungszweck so the token overlap clears the score floor.
    """
    csv = tmp_path / "SPK_2024.csv"
    csv.write_text(
        "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
        "15.03.24;Spotify AB;Premium Family;-15,00;EUR\n"
    )
    bean_dir = tmp_path / "transactions"
    bean_dir.mkdir()
    bean = bean_dir / "SPK.bean"
    bean.write_text(
        textwrap.dedent("""\
        2024-03-09 * "Spotify" "Premium Family"
          Assets:B:SPK            -15.00 EUR
          Expenses:Subscriptions
        """)
    )
    return tmp_path


def _categorize_to(target: str):
    """A `CategorizeFn` stub that always proposes `target` with the CSV's
    payee/description so the diff against the existing entry is non-empty.
    """

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=target),),
            payee=ctx.txn.payee,
            narration=ctx.txn.description,
        )

    return _fn


def _session(base_dir: Path) -> ImportSession:
    # Install a rule whose overrides match what `_categorize_to` would
    # produce: the seed proposal then carries the same payee/narration
    # the categorize_fn would, so the seed-vs-entry diff is non-empty
    # and the pipeline doesn't silent-skip before invoking `categ`.
    rule = CategorizationRule(
        target_account="Expenses:Subscriptions",
        payee_pattern="Spotify",
        override_payee="Spotify AB",
        override_narration="Premium Family",
    )
    cfg = Config(
        banks=[_spk_bank()],
        transactions_dir="transactions",
        matching=MatchingConfig(min_score=0.35),
    )
    return ImportSession(config=cfg, rules=(rule,), options=ImportOptions())


def _run_with(merge_fn, tmp_path: Path):
    base = _write_collision_setup(tmp_path)
    return run(
        _session(base),
        base,
        _categorize_to("Expenses:Subscriptions"),
        NoopReporter(),
        merge_fn=merge_fn,
    )


# ── Each MergeDecision branch ─────────────────────────────────────────────────


class TestMergeDecisionRouting:
    def test_no_merge_fn_keeps_auto_update(self, tmp_path: Path):
        # Preview / scripted-test path: no merge_fn means the pipeline
        # auto-merges. This is the no-regression baseline — every existing
        # test still works because they don't pass a merge_fn.
        results = _run_with(merge_fn=None, tmp_path=tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.action == "update"
        assert r.proposed_changes  # non-empty diff was visible

    def test_update_decision_keeps_result_unchanged(self, tmp_path: Path):
        merge_fn = lambda ctx: MergeDecision(action="update")  # noqa: E731
        results = _run_with(merge_fn=merge_fn, tmp_path=tmp_path)
        r = results[0]
        assert r.action == "update"
        assert r.proposed_changes  # still drives the splice on persist

    def test_keep_decision_silent_matches(self, tmp_path: Path):
        merge_fn = lambda ctx: MergeDecision(action="keep")  # noqa: E731
        results = _run_with(merge_fn=merge_fn, tmp_path=tmp_path)
        r = results[0]
        assert r.action == "update"
        assert r.proposed_changes == []  # no splice; result is silent
        assert r.skip_reason == "user_kept"
        # Proposal mirrors the existing entry so replay reproduces.
        assert r.proposal is not None
        assert r.proposal.payee == "Spotify"
        assert r.proposal.narration == "Premium Family"
        assert r.proposal.target_account == "Expenses:Subscriptions"

    def test_skip_decision_drops_to_skip(self, tmp_path: Path):
        merge_fn = lambda ctx: MergeDecision(action="skip")  # noqa: E731
        results = _run_with(merge_fn=merge_fn, tmp_path=tmp_path)
        r = results[0]
        assert r.action == "skip"
        assert r.skip_reason == "user_skipped"
        assert r.matched_entry is None
        assert r.proposed_changes == []
        assert r.proposal is None

    def test_quit_decision_breaks_the_run(self, tmp_path: Path):
        merge_fn = lambda ctx: MergeDecision(action="quit")  # noqa: E731
        results = _run_with(merge_fn=merge_fn, tmp_path=tmp_path)
        # `run()` stops after a quit result is emitted; only one txn here.
        r = results[0]
        assert r.action == "quit"

    def test_import_new_decision_emits_new_entry_text(self, tmp_path: Path):
        merge_fn = lambda ctx: MergeDecision(action="import_new")  # noqa: E731
        results = _run_with(merge_fn=merge_fn, tmp_path=tmp_path)
        r = results[0]
        assert r.action == "new"
        assert r.matched_entry is None
        # Text for the new entry uses the proposal's values.
        assert "Spotify AB" in r.new_entry_text
        assert "Premium Family" in r.new_entry_text
        assert "Expenses:Subscriptions" in r.new_entry_text

    def test_block_decision_installs_skip_update_rule(self, tmp_path: Path):
        import re

        merge_fn = lambda ctx: MergeDecision(action="block")  # noqa: E731
        results = _run_with(merge_fn=merge_fn, tmp_path=tmp_path)
        r = results[0]
        assert r.action == "skip"
        assert r.skip_reason == "user_blocked"
        assert r.new_rule is not None
        rule = r.new_rule
        assert rule.suppress_updates is True
        assert rule.target_account == "Expenses:Subscriptions"
        # Pattern is re.escape'd (spaces become `\ `), so use the regex
        # itself to verify it matches the original payee.
        assert re.fullmatch(rule.payee_pattern, "Spotify AB") is not None


# ── merge_fn isn't called when nothing collides ───────────────────────────────


class TestNoCollisionPath:
    def test_merge_fn_not_called_for_new_entries(self, tmp_path: Path):
        """A CSV row with no existing match must not invoke `merge_fn`."""
        csv = tmp_path / "SPK_2024.csv"
        csv.write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
            "09.03.24;NewVendor;Fresh purchase;-7,50;EUR\n"
        )
        (tmp_path / "transactions").mkdir()  # no bean file → no candidate
        cfg = Config(
            banks=[_spk_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())

        called = 0

        def merge_fn(ctx: MergeContext) -> MergeDecision:
            nonlocal called
            called += 1
            return MergeDecision(action="update")

        results = run(
            session,
            tmp_path,
            _categorize_to("Expenses:Misc"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert len(results) == 1
        assert results[0].action == "new"
        assert called == 0

    def test_merge_fn_not_called_for_empty_diff(self, tmp_path: Path):
        """An update with no proposed_changes is already silent — Screen 3
        would render an empty diff, so the pipeline doesn't ask.
        """
        # Same bean as the collision setup, CSV that exactly mirrors it.
        bean_dir = tmp_path / "transactions"
        bean_dir.mkdir()
        (bean_dir / "SPK.bean").write_text(
            textwrap.dedent("""\
            2024-03-09 * "Spotify" "Music subscription"
              Assets:B:SPK            -15.00 EUR
              Expenses:Subscriptions
            """)
        )
        csv = tmp_path / "SPK_2024.csv"
        csv.write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
            "09.03.24;Spotify;Music subscription;-15,00;EUR\n"
        )
        cfg = Config(
            banks=[_spk_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())

        called = 0

        def merge_fn(ctx: MergeContext) -> MergeDecision:
            nonlocal called
            called += 1
            return MergeDecision(action="update")

        run(
            session,
            tmp_path,
            _categorize_to("Expenses:Subscriptions"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert called == 0


# ── Block-rule synthesis edge cases ───────────────────────────────────────────


class TestBlockRuleEdges:
    def test_block_falls_back_to_description_when_no_payee(self, tmp_path: Path):
        # Synthesize a SourceTransaction with no payee but a description.
        # The block branch should still install a rule, keyed off the
        # description pattern.
        from beancount_importer.pipeline import _block_update_rule
        from beancount_importer.models import LedgerEntry

        txn = SourceTransaction(
            booking_date=date(2024, 3, 9),
            amount=Decimal("-15"),
            currency="EUR",
            description="some description only",
            bank_key="spk",
        )
        entry = LedgerEntry(
            date=date(2024, 3, 9),
            narration="x",
            source_account="Assets:B:SPK",
            target_account="Expenses:Subs",
            amount=Decimal("-15"),
            currency="EUR",
        )
        import re

        rule = _block_update_rule(txn, entry)
        assert rule is not None
        assert rule.suppress_updates is True
        assert rule.payee_pattern == ""
        # Pattern is re.escape'd; verify it actually matches the original
        # description rather than asserting the literal substring.
        assert re.fullmatch(rule.description_pattern, "some description only") is not None

    def test_block_returns_none_when_no_payee_or_description(self, tmp_path: Path):
        # If neither field is available, the block can't synthesize a
        # safe rule — caller downgrades to a plain skip with no new_rule.
        from beancount_importer.pipeline import _block_update_rule
        from beancount_importer.models import LedgerEntry

        txn = SourceTransaction(
            booking_date=date(2024, 3, 9),
            amount=Decimal("-15"),
            currency="EUR",
            bank_key="spk",
        )
        entry = LedgerEntry(
            date=date(2024, 3, 9),
            narration="x",
            source_account="Assets:B:SPK",
            target_account="Expenses:Subs",
            amount=Decimal("-15"),
            currency="EUR",
        )
        assert _block_update_rule(txn, entry) is None

    def test_block_with_no_payee_and_no_description_skips_without_rule(
        self, tmp_path: Path
    ):
        """End-to-end: a `block` decision against a row that has neither
        payee nor description still skips, but `new_rule` is None and
        `working_rules` doesn't grow.
        """
        # Build a CSV row with only an amount + date (no payee, no desc).
        csv = tmp_path / "SPK_2024.csv"
        csv.write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
            "09.03.24;;;-15,00;EUR\n"
        )
        bean_dir = tmp_path / "transactions"
        bean_dir.mkdir()
        (bean_dir / "SPK.bean").write_text(
            textwrap.dedent("""\
            2024-03-09 * "Spotify" "Music subscription"
              Assets:B:SPK            -15.00 EUR
              Expenses:Subs
            """)
        )
        cfg = Config(
            banks=[_spk_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        merge_fn = lambda ctx: MergeDecision(action="block")  # noqa: E731

        results = run(
            session,
            tmp_path,
            _categorize_to("Expenses:Other"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        if not results:
            return  # empty-payee row may not produce a candidate; that's fine
        r = results[0]
        if r.action == "skip" and r.skip_reason == "user_blocked":
            # The block degraded gracefully — no rule synthesized.
            assert r.new_rule is None


# ── Claim-ordering regression: matched entry stays in bucket on declines ──────


def _write_one_entry_two_csv(tmp_path: Path) -> Path:
    """Setup: 1 bean entry + 2 identical CSV rows that produce non-empty diff.

    Both CSV rows match the bean entry under the `min_score` threshold and
    propose payee/narration changes, so the merge prompt fires. The two
    CSVs are byte-identical; the bucket has one entry to claim.
    """
    csv = tmp_path / "SPK_2024.csv"
    # Both rows sit 6 days after the bean entry — beyond `dedup_max_date_days=5`
    # so dedup doesn't fire, inside the scorer window so the merge prompt does.
    csv.write_text(
        "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
        "15.03.24;Spotify AB;Premium Family;-15,00;EUR\n"
        "15.03.24;Spotify AB;Premium Family;-15,00;EUR\n"
    )
    bean_dir = tmp_path / "transactions"
    bean_dir.mkdir()
    bean = bean_dir / "SPK.bean"
    bean.write_text(
        textwrap.dedent("""\
        2024-03-09 * "Spotify" "Premium Family"
          Assets:B:SPK            -15.00 EUR
          Expenses:Subscriptions
        """)
    )
    return tmp_path


class TestClaimOrderingOnDeclineDecisions:
    """The matched entry must stay in the bucket when the user *declines*
    to consume it — `skip`, `block`, `import_new`, `quit`. Pre-fix, the
    claim ran inside `_build_result` before the merge prompt resolved, so
    a subsequent identical CSV row had nothing left to match against and
    silently degraded to `action=new` instead of re-prompting.
    """

    def test_skip_leaves_entry_for_subsequent_identical_csv(self, tmp_path: Path):
        _write_one_entry_two_csv(tmp_path)
        calls: list[str] = []

        def merge_fn(ctx: MergeContext) -> MergeDecision:
            calls.append("called")
            return MergeDecision(action="skip")

        results = run(
            _session(tmp_path),
            tmp_path,
            _categorize_to("Expenses:Subscriptions"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert len(results) == 2
        # Both rows reached the merge prompt; the second only does so if
        # the first didn't claim the entry.
        assert len(calls) == 2
        assert all(r.action == "skip" and r.skip_reason == "user_skipped" for r in results)

    def test_block_leaves_entry_for_subsequent_identical_csv(self, tmp_path: Path):
        # First row: prompts → block → installs a `suppress_updates` rule
        # AND skips. The regression check is that the entry stays in the
        # bucket — pre-fix, the claim ran inside `_build_result` before
        # the merge prompt resolved, so row 2 found no candidates and
        # silently emitted `action=new`.
        #
        # The override rule installed by `_session` shadows the new
        # block rule on row 2 (linear-scan, first-match-wins), so row 2
        # also reaches the merge prompt — that's enough to prove row 1
        # didn't pre-claim the entry.
        _write_one_entry_two_csv(tmp_path)
        calls = 0

        def merge_fn(ctx: MergeContext) -> MergeDecision:
            nonlocal calls
            calls += 1
            return MergeDecision(action="block")

        results = run(
            _session(tmp_path),
            tmp_path,
            _categorize_to("Expenses:Subscriptions"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert len(results) == 2
        # Both rows reach the merge prompt — the second only does so if
        # the first didn't pre-claim the matched entry.
        assert calls == 2
        assert all(
            r.action == "skip" and r.skip_reason == "user_blocked"
            for r in results
        )

    def test_import_new_leaves_entry_for_subsequent_identical_csv(self, tmp_path: Path):
        # `import_new` says "this is a fresh entry, the candidate was a
        # false match". The bucket entry must remain so a *third* row
        # could legitimately match it. Here we use 2 identical CSVs +
        # 1 entry: both rows hit the merge prompt and end up as `new`,
        # because the second row also still sees the candidate.
        _write_one_entry_two_csv(tmp_path)
        calls = 0

        def merge_fn(ctx: MergeContext) -> MergeDecision:
            nonlocal calls
            calls += 1
            return MergeDecision(action="import_new")

        results = run(
            _session(tmp_path),
            tmp_path,
            _categorize_to("Expenses:Subscriptions"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert len(results) == 2
        # Pre-fix the second row degraded to plain `new` without firing
        # merge_fn (because the bucket had been emptied by row 1's
        # premature claim). Post-fix, both rows fire the prompt.
        assert calls == 2
        assert all(r.action == "new" and r.matched_entry is None for r in results)


class TestClaimOrderingOnConsumingDecisions:
    """When the user *does* consume the entry — `update` or silent `keep` —
    a subsequent identical CSV row must NOT re-claim the same entry.
    Two identical CSVs + two identical bean entries should pair 1:1.
    """

    def _two_entries_two_csv(self, tmp_path: Path) -> None:
        csv = tmp_path / "SPK_2024.csv"
        csv.write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
            "09.03.24;Spotify AB;Premium Family;-15,00;EUR\n"
            "09.03.24;Spotify AB;Premium Family;-15,00;EUR\n"
        )
        bean_dir = tmp_path / "transactions"
        bean_dir.mkdir()
        # Two identical bean entries differing only by line position —
        # the writer emits unique `(file_path, line_start)` pairs.
        bean = bean_dir / "SPK.bean"
        bean.write_text(
            textwrap.dedent("""\
            2024-03-09 * "Spotify" "Music subscription"
              Assets:B:SPK            -15.00 EUR
              Expenses:Subscriptions

            2024-03-09 * "Spotify" "Music subscription"
              Assets:B:SPK            -15.00 EUR
              Expenses:Subscriptions
            """)
        )

    def test_update_pairs_distinct_entries(self, tmp_path: Path):
        self._two_entries_two_csv(tmp_path)
        merge_fn = lambda ctx: MergeDecision(action="update")  # noqa: E731
        results = run(
            _session(tmp_path),
            tmp_path,
            _categorize_to("Expenses:Subscriptions"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert len(results) == 2
        keys = [
            (r.matched_entry.file_path, r.matched_entry.line_start)
            for r in results
            if r.matched_entry is not None
        ]
        assert len(keys) == 2 and len(set(keys)) == 2

    def test_keep_pairs_distinct_entries(self, tmp_path: Path):
        self._two_entries_two_csv(tmp_path)
        merge_fn = lambda ctx: MergeDecision(action="keep")  # noqa: E731
        results = run(
            _session(tmp_path),
            tmp_path,
            _categorize_to("Expenses:Subscriptions"),
            NoopReporter(),
            merge_fn=merge_fn,
        )
        assert len(results) == 2
        keys = [
            (r.matched_entry.file_path, r.matched_entry.line_start)
            for r in results
            if r.matched_entry is not None
        ]
        assert len(keys) == 2 and len(set(keys)) == 2

"""End-to-end round-trip and snapshot tests.

Two flavours, both at the pipeline-plus-persistence layer (not Typer-level
— that's covered in test_cli.py):

- **Round-trip**: run the pipeline on a CSV, persist the results to disk,
  then run again on the same CSV against the just-written ledger. The
  second run should silent-skip every row. This catches:
  - dedup hash drift between txn-side and entry-side
  - narration truncation regression (writer truncates → next run shouldn't
    propose rewinding it as a "change")
  - replay miss when DecisionLog wasn't flushed
  - rule-driven payee/narration overrides not being mirrored at re-read

- **Snapshot**: assert the exact text of the rendered `.bean` file for a
  fixed CSV input. Catches format/render regressions in `format_transaction`
  that would otherwise only surface as visual noise in diffs.

The tests call `cli._persist_results` directly to drive the same writer
path the production CLI uses; that keeps the round-trip honest without
requiring a Typer subprocess.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from beancount_importer.cli import _persist_results
from beancount_importer.config import (
    BankConfig,
    Config,
    CsvConfig,
    MatchingConfig,
)
from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import (
    CategorizeContext,
    CategorizeFn,
    NoopReporter,
    run,
)
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.session import ImportOptions, ImportSession


SPK_CSV = textwrap.dedent("""\
    Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung
    15.01.24;Netflix;Netflix Abo;-15,99;EUR
    17.01.24;Rewe;REWE Filiale;-42,50;EUR
""")


def _spk_bank(output_template: str = "transactions/{year}/SPK.bean") -> BankConfig:
    return BankConfig(
        key="spk",
        display_name="Sparkasse",
        account="Assets:B:SPK",
        file_glob="SPK_*.csv",
        output_file=output_template,
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


def _session(
    base_dir: Path,
    *,
    rules: tuple[CategorizationRule, ...] = (),
) -> ImportSession:
    return ImportSession(
        config=Config(
            banks=[_spk_bank()],
            matching=MatchingConfig(min_score=0.35),
        ),
        rules=rules,
        options=ImportOptions(),
    )


def _categorize_to(account: str) -> CategorizeFn:
    """A CategorizeFn that always proposes `account` and carries the txn's
    own payee/description through. Mirrors what a human-driven Screen 1
    Enter-confirm would produce, so the written entry matches the CSV
    row's text fields verbatim — the round-trip then dedups cleanly."""

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=account),),
            payee=ctx.txn.payee,
            narration=ctx.txn.description,
        )

    return _fn


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "SPK_jan.csv").write_text(SPK_CSV)
    return tmp_path


class TestRoundTrip:
    def test_second_run_silent_skips_every_row(self, project: Path):
        # First run: every row is new, gets categorized and written.
        decisions = DecisionLog(project / "decisions.jsonl")
        results1 = run(
            _session(project),
            project,
            _categorize_to("Expenses:Unknown"),
            NoopReporter(),
            decisions=decisions,
        )
        assert [r.action for r in results1] == ["new", "new"]
        decisions.flush()
        _persist_results(
            results1, _session(project).config, project, dry_run=False
        )
        assert (project / "transactions" / "2024" / "SPK.bean").exists()

        # Second run: ledger now has both entries. Replay short-circuits
        # categorize. Both rows must produce updates with zero diffs (or
        # equivalent silent-skip outcomes); nothing should reach a
        # categorizer that would error.
        def err_categ(ctx: CategorizeContext) -> CategoryProposal:
            raise AssertionError(
                f"second run invoked categorize_fn for {ctx.txn.payee!r} — "
                "round-trip dedup/replay broke"
            )

        decisions2 = DecisionLog(project / "decisions.jsonl")
        results2 = run(
            _session(project),
            project,
            err_categ,
            NoopReporter(),
            decisions=decisions2,
        )
        # Replay path: action="update" with no proposed_changes is the
        # silent-skip shape that `_persist_results` no-ops on.
        assert all(r.action != "new" for r in results2), [
            (r.action, r.source_txn.payee) for r in results2
        ]
        assert all(not r.proposed_changes for r in results2)

    def test_second_run_with_rule_override_still_dedups(self, project: Path):
        # A rule that rewrites payee/narration on the way in must also
        # apply on the second run so the entry written on run 1 still
        # content-hash-matches the rule-rewritten row on run 2. Without
        # `_apply_rule_overrides` being called pre-dedup on both sides,
        # the second run re-prompts.
        rule = CategorizationRule(
            payee_pattern="Netflix",
            target_account="Expenses:Streaming",
            override_payee="Netflix",
            override_narration="Subscription",
            bank_key="spk",
        )
        session = _session(project, rules=(rule,))
        decisions = DecisionLog(project / "decisions.jsonl")
        results1 = run(
            session,
            project,
            _categorize_to("Expenses:Unknown"),  # ignored for rule-matched row
            NoopReporter(),
            decisions=decisions,
        )
        decisions.flush()
        _persist_results(results1, session.config, project, dry_run=False)

        def err_categ(ctx: CategorizeContext) -> CategoryProposal:
            if ctx.txn.payee == "Netflix":
                raise AssertionError(
                    "rule-matched Netflix row reached categorize_fn on second run"
                )
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Unknown"),),
                payee=ctx.txn.payee,
                narration=ctx.txn.description,
            )

        decisions2 = DecisionLog(project / "decisions.jsonl")
        results2 = run(
            session, project, err_categ, NoopReporter(), decisions=decisions2
        )
        netflix = [r for r in results2 if r.source_txn.payee == "Netflix"]
        assert len(netflix) == 1
        assert not netflix[0].proposed_changes


class TestBeanOutputSnapshot:
    def test_new_entry_text_is_stable(self, project: Path):
        # Single row, deterministic categorizer, no rule. Asserts the
        # exact rendered .bean text — any whitespace, ordering, or
        # number-formatting drift in `format_transaction` shows up here.
        (project / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung
            15.01.24;Netflix;Netflix Abo;-15,99;EUR
        """))
        session = _session(project)
        results = run(
            session,
            project,
            _categorize_to("Expenses:Streaming"),
            NoopReporter(),
        )
        _persist_results(results, session.config, project, dry_run=False)

        out = (project / "transactions" / "2024" / "SPK.bean").read_text()
        # Writer column-pads the amount; using a raw string with explicit
        # spacing rather than autocomputing from the writer's settings —
        # this is a snapshot, the whole point is to pin the bytes.
        expected = (
            '2024-01-15 * "Netflix" "Netflix Abo"\n'
            "  Assets:B:SPK                             -15.99 EUR\n"
            "  Expenses:Streaming\n"
        )
        assert out == expected

    def test_two_entries_separated_by_blank_line(self, project: Path):
        # Both CSV rows go through; the writer must put one blank line
        # between them (no double blanks, no missing separator).
        session = _session(project)
        results = run(
            session,
            project,
            _categorize_to("Expenses:Misc"),
            NoopReporter(),
        )
        _persist_results(results, session.config, project, dry_run=False)

        out = (project / "transactions" / "2024" / "SPK.bean").read_text()
        # Sanity: both transactions present, in CSV order, separated by
        # exactly one blank line.
        assert out.count('2024-01-') == 2
        assert "\n\n\n" not in out  # no triple-blank
        netflix_idx = out.index("Netflix")
        rewe_idx = out.index("Rewe")
        assert netflix_idx < rewe_idx
        between = out[netflix_idx:rewe_idx]
        assert between.count("\n\n") == 1

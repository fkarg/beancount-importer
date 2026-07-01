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
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from beancount_importer.cli import _persist_results
from beancount_importer.config import (
    BankConfig,
    Config,
    CsvConfig,
    MatchingConfig,
)
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
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


class TestPersistUpdateOrdering:
    """Regression for the 2024 ledger corruption: multiple updates to the same
    file were spliced one at a time in results order (top-down) using line
    numbers captured when the ledger was loaded. Each splice that grew an
    entry shifted every later target down, so subsequent splices landed on —
    and rewrote — whatever transaction now sat at the stale coordinates.
    `_persist_results` must apply a file's updates bottom-up.
    """

    def _entry(self, f: Path, line_start: int, d: date, payee: str,
               amount: str, old_account: str) -> LedgerEntry:
        return LedgerEntry(
            date=d,
            payee=payee,
            narration="orig",
            source_account="Assets:B:SPK",
            target_account=old_account,
            amount=Decimal(amount),
            line_start=line_start,
            file_path=str(f),
        )

    def _update(self, entry: LedgerEntry, new_account: str) -> ImportResult:
        return ImportResult(
            source_txn=SourceTransaction(
                booking_date=entry.date, amount=entry.amount, bank_key="spk"
            ),
            action="update",
            matched_entry=entry,
            proposal=CategoryProposal(
                action="categorize",
                postings=(Posting(account=new_account),),
                # Metadata makes the replacement one line longer than the
                # original — the growth that shifts everything below it.
                metadata={"note": "recategorized"},
            ),
            proposed_changes=[
                ProposedChange("target_account", entry.target_account, new_account)
            ],
        )

    def test_updates_in_top_down_order_do_not_corrupt_later_targets(
        self, tmp_path: Path
    ):
        f = tmp_path / "SPK.bean"
        f.write_text(
            '2024-01-10 * "A" "orig"\n'          # line 1
            "  Assets:B:SPK   -1.00 EUR\n"
            "  Expenses:OldA   1.00 EUR\n"
            "\n"
            '2024-01-11 * "B" "orig"\n'          # line 5
            "  Assets:B:SPK   -2.00 EUR\n"
            "  Expenses:OldB   2.00 EUR\n"
            "\n"
            '2024-01-12 * "C" "orig"\n'          # line 9
            "  Assets:B:SPK   -3.00 EUR\n"
            "  Expenses:OldC   3.00 EUR\n"
        )
        results = [
            # Deliberately top-down: A's splice grows the file by one line,
            # invalidating B's line_start=5 unless persistence reorders.
            self._update(
                self._entry(f, 1, date(2024, 1, 10), "A", "-1.00", "Expenses:OldA"),
                "Expenses:NewA",
            ),
            self._update(
                self._entry(f, 5, date(2024, 1, 11), "B", "-2.00", "Expenses:OldB"),
                "Expenses:NewB",
            ),
        ]
        config = Config(banks=[_spk_bank()], matching=MatchingConfig(min_score=0.35))
        failures = _persist_results(results, config, tmp_path, dry_run=False)
        assert failures == []

        out = f.read_text()
        # Both updates landed; neither original categorization survives.
        assert "Expenses:NewA" in out
        assert "Expenses:NewB" in out
        assert "Expenses:OldA" not in out
        assert "Expenses:OldB" not in out
        # B was rewritten in place, not duplicated at stale coordinates.
        assert out.count("2024-01-11") == 1
        # The untouched neighbour C survives byte-for-byte.
        assert (
            '2024-01-12 * "C" "orig"\n'
            "  Assets:B:SPK   -3.00 EUR\n"
            "  Expenses:OldC   3.00 EUR\n"
        ) in out


class TestCollapseRoundTrip:
    """End-to-end via-paypal collapse: a PayPal purchase row matching an
    on-disk SPK→PayPal transfer rewrites the transfer into the collapsed
    single-transaction form; a second run over the same CSV recognizes the
    `paypal:` metadata as settlement evidence and touches nothing.
    """

    XFER = (
        '2024-05-15 * "Steam" "PayPal" ^xfer-spk-paypal-1\n'
        "  Assets:B:SPK  -33.82 EUR\n"
        "  Assets:B:PayPal\n"
    )

    def _paypal_bank(self) -> BankConfig:
        return BankConfig(
            key="paypal",
            display_name="PayPal",
            account="Assets:B:PayPal",
            file_glob="PayPal_*.csv",
            output_file="transactions/PAYPAL.bean",
            csv=CsvConfig(
                delimiter=",",
                date_format=["%Y-%m-%d"],
                amount_locale="en",
                field_date="Date",
                field_amount="Gross",
                field_currency="Currency",
                field_description="Description",
            ),
        )

    def _config(self) -> Config:
        return Config(
            banks=[self._paypal_bank()],
            transactions_dir="transactions",
            paypal_account="Assets:B:PayPal",
            matching=MatchingConfig(
                min_score=0.35,
                synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
            ),
        )

    def test_collapse_persists_and_second_run_settles(self, tmp_path: Path):
        (tmp_path / "transactions").mkdir()
        spk = tmp_path / "transactions" / "SPK.bean"
        spk.write_text(self.XFER)
        (tmp_path / "PayPal_2024.csv").write_text(
            "Date,Description,Currency,Gross\n"
            "2024-05-13,Steam,EUR,-33.82\n"
        )
        config = self._config()
        session = ImportSession(config=config, options=ImportOptions())
        results = run(
            session, tmp_path, _categorize_to("Expenses:Games"), NoopReporter()
        )
        assert [r.action for r in results] == ["update"]
        failures = _persist_results(results, config, tmp_path, dry_run=False)
        assert failures == []

        out = spk.read_text()
        assert "Assets:B:PayPal" not in out
        assert "Expenses:Games" in out
        assert "    paypal: 2024-05-13" in out.splitlines()
        assert "^xfer-spk-paypal-1" in out.splitlines()[0]
        # The purchase was consumed into SPK.bean — no PayPal-side entry.
        assert not (tmp_path / "transactions" / "PAYPAL.bean").exists()

        # Second run over the same CSV: the collapsed entry's `paypal:`
        # metadata marks the row as settled before the scorer runs.
        def err_categ(ctx: CategorizeContext) -> CategoryProposal:
            raise AssertionError("second run re-prompted a collapsed row")

        results2 = run(
            ImportSession(config=self._config(), options=ImportOptions()),
            tmp_path,
            err_categ,
            NoopReporter(),
        )
        assert len(results2) == 1
        assert results2[0].action in ("skip", "update")
        assert not results2[0].proposed_changes
        assert spk.read_text() == out


class TestViaPaypalPlaceholderRoundTrip:
    """Placeholder lifecycle on disk: `via_paypal: TRUE` → PayPal import
    upgrades it to posting-level `paypal: <date>` (marker gone, sepa_ref
    kept, no PayPal-side entry) → a re-run of the same CSV settles silently.
    """

    PLACEHOLDER = (
        '2024-04-15 * "Penny" "PayPal"\n'
        "  Assets:B:SPK  -7.81 EUR\n"
        "    via_paypal: TRUE\n"
        '    sepa_ref: "REF-P"\n'
        "  Expenses:Food:Groceries  7.81 EUR\n"
    )

    def _paypal_bank(self) -> BankConfig:
        return BankConfig(
            key="paypal",
            display_name="PayPal",
            account="Assets:B:PayPal",
            file_glob="PayPal_*.csv",
            output_file="transactions/PAYPAL.bean",
            csv=CsvConfig(
                delimiter=",",
                date_format=["%Y-%m-%d"],
                amount_locale="en",
                field_date="Date",
                field_amount="Gross",
                field_currency="Currency",
                field_description="Description",
            ),
        )

    def _config(self) -> Config:
        return Config(
            banks=[self._paypal_bank()],
            transactions_dir="transactions",
            paypal_account="Assets:B:PayPal",
            matching=MatchingConfig(
                min_score=0.35,
                synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
            ),
        )

    def test_placeholder_upgrade_persists_and_settles(self, tmp_path: Path):
        (tmp_path / "transactions").mkdir()
        spk = tmp_path / "transactions" / "SPK.bean"
        spk.write_text(self.PLACEHOLDER)
        (tmp_path / "PayPal_2024.csv").write_text(
            "Date,Description,Currency,Gross\n"
            "2024-04-13,Penny Markt,EUR,-7.81\n"
        )
        config = self._config()
        session = ImportSession(config=config, options=ImportOptions())

        def err_categ(ctx: CategorizeContext) -> CategoryProposal:
            raise AssertionError("placeholder link must not prompt")

        results = run(session, tmp_path, err_categ, NoopReporter())
        assert [r.action for r in results] == ["update"]
        failures = _persist_results(results, config, tmp_path, dry_run=False)
        assert failures == []

        out = spk.read_text()
        assert "via_paypal" not in out
        assert "    paypal: 2024-04-13" in out.splitlines()
        assert 'sepa_ref: "REF-P"' in out
        assert "Expenses:Food:Groceries" in out
        assert not (tmp_path / "transactions" / "PAYPAL.bean").exists()

        results2 = run(
            ImportSession(config=self._config(), options=ImportOptions()),
            tmp_path,
            err_categ,
            NoopReporter(),
        )
        assert len(results2) == 1
        assert results2[0].action in ("skip", "update")
        assert not results2[0].proposed_changes
        assert spk.read_text() == out

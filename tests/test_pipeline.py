"""End-to-end pipeline tests using a deterministic CategorizeFn."""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from beancount_importer.config import (
    BankConfig,
    Config,
    CsvConfig,
    MatchingConfig,
    SkipUpdatePattern,
)
from beancount_importer.models import (
    CategoryProposal,
    Posting,
)
from beancount_importer.pipeline import (
    CategorizeContext,
    NoopReporter,
    compute_bean_provenance_stats,
    run,
)
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag, TagState
from beancount_importer.session import ImportOptions, ImportSession


# ── Fixtures ──────────────────────────────────────────────────────────────────


def write_spk_csv(path: Path) -> None:
    path.write_text(textwrap.dedent("""\
        Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
        15.01.24;Netflix;Netflix Abo;-15,99;EUR;NETFLIX-001
        16.01.24;Rewe;REWE Filiale;-42,50;EUR;
        17.01.24;Salary;Gehalt Januar;3000,00;EUR;SALARY-JAN
    """))


def make_spk_bank(year_template_output: bool = True) -> BankConfig:
    out = "transactions/{year}/SPK.bean" if year_template_output else "spk.bean"
    return BankConfig(
        key="spk",
        display_name="Sparkasse",
        account="Assets:B:SPK",
        file_glob="SPK_*.csv",
        output_file=out,
        csv=CsvConfig(
            delimiter=";",
            date_format=["%d.%m.%y"],
            amount_locale="de",
            field_date="Buchungstag",
            field_amount="Betrag",
            field_currency="Waehrung",
            field_payee="Beguenstigter",
            field_description="Verwendungszweck",
            field_sepa_reference="Kundenreferenz",
        ),
    )


def make_session(
    base_dir: Path,
    *,
    rules: tuple[CategorizationRule, ...] = (),
    tag_state: TagState = TagState(),
    options: ImportOptions = ImportOptions(),
    skip_patterns: tuple[SkipUpdatePattern, ...] = (),
) -> ImportSession:
    cfg = Config(
        banks=[make_spk_bank(year_template_output=False)],
        skip_update_patterns=list(skip_patterns),
        matching=MatchingConfig(min_score=0.35),
    )
    return ImportSession(
        config=cfg,
        rules=rules,
        tag_state=tag_state,
        options=options,
    )


def fixed_categorize(account: str = "Expenses:Unknown"):
    """Build a deterministic CategorizeFn that always picks `account`."""

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=account),),
        )

    return _fn


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    write_spk_csv(tmp_path / "SPK_jan.csv")
    return tmp_path


# ── Smoke: parse + categorize three new transactions ─────────────────────────


class TestPipelineSmoke:
    def test_emits_one_result_per_csv_row(self, base_dir: Path):
        session = make_session(base_dir)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert len(results) == 3

    def test_all_categorize_actions_become_new(self, base_dir: Path):
        session = make_session(base_dir)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert all(r.action == "new" for r in results)

    def test_new_entries_carry_text(self, base_dir: Path):
        session = make_session(base_dir)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert "Netflix" in results[0].new_entry_text
        assert "Assets:B:SPK" in results[0].new_entry_text
        assert "Expenses:Unknown" in results[0].new_entry_text

    def test_proposal_attached_to_result(self, base_dir: Path):
        session = make_session(base_dir)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert results[0].proposal is not None
        assert results[0].proposal.action == "categorize"


# ── Skip / quit ──────────────────────────────────────────────────────────────


class TestPipelineSkipQuit:
    def test_skip_action_yields_skip_result(self, base_dir: Path):
        def categ(ctx: CategorizeContext) -> CategoryProposal:
            return CategoryProposal(action="skip")

        session = make_session(base_dir)
        results = run(session, base_dir, categ, NoopReporter())
        assert all(r.action == "skip" for r in results)

    def test_quit_terminates_iteration(self, base_dir: Path):
        def categ(ctx: CategorizeContext) -> CategoryProposal:
            return CategoryProposal(action="quit")

        session = make_session(base_dir)
        results = run(session, base_dir, categ, NoopReporter())
        # First txn returns quit; pipeline halts.
        assert len(results) == 1
        assert results[0].action == "quit"


# ── Rule matching ────────────────────────────────────────────────────────────


class TestPipelineRules:
    def test_rule_match_recorded_on_result(self, base_dir: Path):
        rule = CategorizationRule(
            target_account="Expenses:Streaming",
            payee_pattern="Netflix",
        )
        # Categorizer must still run; we just check rule_matched gets attached.
        session = make_session(base_dir, rules=(rule,))
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        netflix = next(r for r in results if r.source_txn.payee == "Netflix")
        assert netflix.rule_matched is not None
        assert netflix.rule_matched.target_account == "Expenses:Streaming"

    def test_save_as_rule_extends_working_rules(self, base_dir: Path):
        # Two transactions from the same payee: the categorizer flags the first
        # as save_as_rule. The second should see the synthesized rule already.
        seen_rules_count = []

        def categ(ctx: CategorizeContext) -> CategoryProposal:
            seen_rules_count.append(len(ctx.rules))
            save = ctx.txn.payee == "Netflix"
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:X"),),
                save_as_rule=save,
            )

        # Rewrite CSV so two rows share payee=Netflix
        (base_dir / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            15.01.24;Netflix;Netflix Abo;-15,99;EUR;A
            16.01.24;Netflix;Netflix Abo;-15,99;EUR;B
            17.01.24;Other;Foo;-1,00;EUR;C
        """))

        session = make_session(base_dir)
        results = run(session, base_dir, categ, NoopReporter())
        assert results[0].new_rule is not None
        # By the time the third txn was visited, working_rules had grown.
        assert seen_rules_count[2] > seen_rules_count[0]


# ── Skip-update patterns ─────────────────────────────────────────────────────


class TestPipelineSkipPatterns:
    def test_payee_pattern_drops_proposal(self, base_dir: Path):
        session = make_session(
            base_dir,
            skip_patterns=(SkipUpdatePattern(field="payee", pattern="^Rewe"),),
        )
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        rewe = next(r for r in results if r.source_txn.payee == "Rewe")
        assert rewe.action == "skip"
        # Other transactions still process normally
        assert any(r.action == "new" for r in results)

    def test_description_pattern(self, base_dir: Path):
        session = make_session(
            base_dir,
            skip_patterns=(SkipUpdatePattern(field="description", pattern="REWE"),),
        )
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        rewe = next(r for r in results if r.source_txn.payee == "Rewe")
        assert rewe.action == "skip"


# ── Active tag threading ─────────────────────────────────────────────────────


class TestPipelineActiveTag:
    def test_always_tag_applied_to_all(self, base_dir: Path):
        ts = TagState(active=ActiveTag(tag="trip-berlin", mode="always"))
        session = make_session(base_dir, tag_state=ts)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        new_results = [r for r in results if r.action == "new"]
        assert all(r.proposal is not None and r.proposal.tag == "trip-berlin" for r in new_results)

    def test_once_tag_clears_after_first(self, base_dir: Path):
        ts = TagState(active=ActiveTag(tag="lunch", mode="once"))
        session = make_session(base_dir, tag_state=ts)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        # First new transaction tagged; subsequent ones not.
        new_results = [r for r in results if r.action == "new"]
        assert new_results[0].proposal is not None
        assert new_results[0].proposal.tag == "lunch"
        assert new_results[1].proposal is not None
        assert new_results[1].proposal.tag is None
        # Delta emitted on the first result reporting the clear.
        assert new_results[0].tag_state_delta is not None
        assert new_results[0].tag_state_delta.op == "clear"

    def test_duration_tag_skips_outside_window(self, base_dir: Path):
        ts = TagState(active=ActiveTag(
            tag="trip",
            mode="duration",
            from_date=date(2024, 1, 16),
            until_date=date(2024, 1, 16),
        ))
        session = make_session(base_dir, tag_state=ts)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        by_date = {r.source_txn.booking_date: r for r in results}
        for d in (date(2024, 1, 15), date(2024, 1, 16), date(2024, 1, 17)):
            assert by_date[d].proposal is not None  # narrow Optional for type-checker
        assert by_date[date(2024, 1, 15)].proposal.tag is None  # type: ignore[union-attr]
        assert by_date[date(2024, 1, 16)].proposal.tag == "trip"  # type: ignore[union-attr]
        assert by_date[date(2024, 1, 17)].proposal.tag is None  # type: ignore[union-attr]


# ── Replay ───────────────────────────────────────────────────────────────────


class TestPipelineReplay:
    def test_replay_short_circuits_categorize(self, base_dir: Path):
        # Pre-seed the decision log with a choice for the first txn.
        log_path = base_dir / "decisions.jsonl"
        log = DecisionLog(log_path)
        # Drive the pipeline once with a deterministic categorizer to seed.
        session = make_session(base_dir)
        run(session, base_dir, fixed_categorize("Expenses:From-Replay"), NoopReporter(), decisions=log)
        # Now reopen log and run again with a categorizer that would error if called.
        log2 = DecisionLog(log_path)
        called = []

        def err_categ(ctx: CategorizeContext) -> CategoryProposal:
            called.append(ctx.txn)
            return CategoryProposal(action="categorize", postings=(Posting(account="X"),))

        results = run(session, base_dir, err_categ, NoopReporter(), decisions=log2)
        # All three replayed; categorizer never invoked.
        assert called == []
        assert all(r.is_replay for r in results if r.action == "new")


# ── Year filter ──────────────────────────────────────────────────────────────


class TestPipelineYearFilter:
    def test_only_keeps_matching_year(self, base_dir: Path):
        # The CSV has three rows in Jan 2024. Add a 2023 row to verify filtering.
        (base_dir / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            15.01.24;Netflix;Netflix Abo;-15,99;EUR;A
            16.01.23;OldThing;Old txn;-1,00;EUR;B
            17.01.24;Other;Foo;-2,00;EUR;C
        """))
        session = make_session(base_dir, options=ImportOptions(year_filter=(2024,)))
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        years = {r.source_txn.booking_date.year for r in results}
        assert years == {2024}
        assert len(results) == 2

    def test_multiple_years(self, base_dir: Path):
        (base_dir / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            15.01.22;A;A;-1,00;EUR;A
            15.01.23;B;B;-1,00;EUR;B
            15.01.24;C;C;-1,00;EUR;C
        """))
        session = make_session(base_dir, options=ImportOptions(year_filter=(2023, 2024)))
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        years = {r.source_txn.booking_date.year for r in results}
        assert years == {2023, 2024}

    def test_no_filter_keeps_all(self, base_dir: Path):
        session = make_session(base_dir, options=ImportOptions(year_filter=None))
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert len(results) == 3


# ── Bank filter ──────────────────────────────────────────────────────────────


class TestPipelineBankFilter:
    def test_bank_filter_skips_others(self, base_dir: Path, tmp_path: Path):
        # Add a second bank with no CSV files; filter to spk only.
        cfg = Config(
            banks=[
                make_spk_bank(year_template_output=False),
                BankConfig(
                    key="n26",
                    display_name="N26",
                    account="Assets:B:N26",
                    file_glob="N26_*.csv",
                    output_file="n26.bean",
                    csv=CsvConfig(field_date="Date", field_amount="Amount"),
                ),
            ],
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(
            config=cfg,
            options=ImportOptions(bank_filter="spk"),
        )
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert all(r.source_txn.bank_key == "spk" for r in results)


# ── Cross-bank matching: PayPal CSV against an SPK→PayPal entry ─────────────


class TestPipelineCrossBank:
    """A PayPal CSV row should match against a transit-leg entry that lives
    in another bank's ledger file. This covers the two main pathways:

    1. The other bank's entry has an explicit `Assets:B:PayPal` posting with
       no number (beancount infers the amount).
    2. The other bank's entry only has a metadata hint (`paypal: <date>`)
       that a user-installed plugin would split at load time.
    """

    def _write_paypal_csv(self, path: Path, row: str) -> None:
        path.write_text(
            "Date,Description,Currency,Gross\n"
            f"{row}\n"
        )

    def _make_paypal_bank(self) -> BankConfig:
        return BankConfig(
            key="paypal",
            display_name="PayPal",
            account="Assets:B:PayPal",
            file_glob="PayPal_*.csv",
            output_file="paypal.bean",
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

    def test_inferred_paypal_leg_matches_csv_row(self, tmp_path: Path):
        # SPK-side entry where PayPal posting amount is inferred.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(
            '2024-04-13 * "Google Payment" "PayPal Einkauf"\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '  Assets:B:PayPal\n'
        )
        # PayPal CSV: same purchase, opposite sign, on the same/nearby date.
        self._write_paypal_csv(
            tmp_path / "PayPal_2024.csv",
            "2024-04-13,Google Payment,EUR,-3.39",
        )
        cfg = Config(
            banks=[self._make_paypal_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        # Cross-bank transit match → no proposed changes → counts as matched.
        assert results[0].action == "update"
        assert results[0].proposed_changes == []
        assert results[0].matched_entry is not None
        assert results[0].matched_entry.amount_inferred is True

    def test_paypal_metadata_synthesizes_match(self, tmp_path: Path):
        # SPK entry with `paypal: <date>` metadata — plugin would split this
        # into a separate PayPal transaction at load time. Without the
        # plugin, the importer reconstructs the virtual entry from config.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n'
        )
        # PayPal CSV records the actual purchase date.
        self._write_paypal_csv(
            tmp_path / "PayPal_2024.csv",
            "2024-04-11,Google Payment,EUR,-3.39",
        )
        cfg = Config(
            banks=[self._make_paypal_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(
                min_score=0.35,
                synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
            ),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        assert results[0].action == "update"
        assert results[0].proposed_changes == []

    def test_funding_bank_can_be_n26(self, tmp_path: Path):
        # Same shape as above but the transit leg lives on N26 instead of
        # SPK — proves the cross-bank matching is bank-agnostic.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "N26.bean").write_text(
            '2024-05-01 * "PayPal" "Top-up"\n'
            '  Assets:B:N26  -50.00 EUR\n'
            '  Assets:B:PayPal\n'
        )
        self._write_paypal_csv(
            tmp_path / "PayPal_2024.csv",
            "2024-05-01,Bank Deposit,EUR,50.00",
        )
        cfg = Config(
            banks=[self._make_paypal_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        assert results[0].action == "update"
        assert results[0].proposed_changes == []


# ── Reverse provenance: bean entries that lack a CSV row ─────────────────────


class TestBeanProvenanceStats:
    def _bank(self) -> BankConfig:
        return make_spk_bank(year_template_output=False)

    def test_counts_total_bean_entries_per_year(self, tmp_path: Path):
        # Two SPK ledger entries in 2024, one in 2023.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR

            2024-02-01 * "Mystery" "Cash withdraw"
              Assets:B:SPK  -50.00 EUR
              Expenses:Cash  50.00 EUR

            2023-12-01 * "Old" "from last year"
              Assets:B:SPK  -1.00 EUR
              Expenses:Misc  1.00 EUR
        """))
        # No CSV files at all.
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        assert stats[("spk", 2024)].total_in_bean == 2
        assert stats[("spk", 2024)].bean_unmatched == 2
        assert stats[("spk", 2023)].total_in_bean == 1
        assert stats[("spk", 2023)].bean_unmatched == 1

    def test_csv_match_reduces_unmatched(self, tmp_path: Path):
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR

            2024-02-01 * "Mystery" "Cash withdraw"
              Assets:B:SPK  -50.00 EUR
              Expenses:Cash  50.00 EUR
        """))
        # CSV row matches the Netflix entry; the cash withdrawal stays unmatched.
        write_spk_csv(tmp_path / "SPK_jan.csv")  # 3 rows in 2024-01
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        assert stats[("spk", 2024)].total_in_bean == 2
        assert stats[("spk", 2024)].bean_unmatched == 1

    def test_year_filter_scopes_results(self, tmp_path: Path):
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "X" ""
              Assets:B:SPK  -1.00 EUR
              Expenses:X  1.00 EUR

            2023-06-01 * "Y" ""
              Assets:B:SPK  -2.00 EUR
              Expenses:Y  2.00 EUR
        """))
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        opts = ImportOptions(year_filter=(2024,))
        session = ImportSession(config=cfg, options=opts)
        stats = compute_bean_provenance_stats(session, tmp_path)
        assert ("spk", 2024) in stats
        assert ("spk", 2023) not in stats

    def test_expanded_count_skipped_without_main_bean(self, tmp_path: Path):
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "X" ""
              Assets:B:SPK  -1.00 EUR
              Expenses:X  1.00 EUR
        """))
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # No `main_bean` configured ⇒ expanded count is reported as zero.
        assert stats[("spk", 2024)].bean_expanded == 0

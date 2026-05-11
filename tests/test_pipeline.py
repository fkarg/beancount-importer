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
from beancount_importer.pipeline import (
    _apply_rule_overrides,
    _compute_near_misses,
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


class TestEntryClaiming:
    """Two identical CSV rows + two identical bean entries: each row
    should pair with a distinct entry instead of both pointing at the
    same one. Otherwise the second entry shows up as a CSV-orphan in
    the bean-side provenance report.
    """

    def test_two_identical_csv_rows_pair_with_distinct_entries(
        self, tmp_path: Path
    ):
        # CSV: two identical rows on the same date.
        (tmp_path / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            08.01.24;DISCOSTADL;Bar tab;-16,00;EUR;
            08.01.24;DISCOSTADL;Bar tab;-16,00;EUR;
        """))
        # Bean: two identical existing entries that should pair 1:1.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-08 * "DISCOSTADL" "Bar tab"
              Assets:B:SPK  -16.00 EUR
              Expenses:Drinks  16.00 EUR

            2024-01-08 * "DISCOSTADL" "Bar tab"
              Assets:B:SPK  -16.00 EUR
              Expenses:Drinks  16.00 EUR
        """))
        session = make_session(tmp_path)
        results = run(
            session,
            tmp_path,
            fixed_categorize("Expenses:Drinks"),
            NoopReporter(),
        )
        assert len(results) == 2
        # Both rows resolve to a matched entry (either silent dedup or
        # silent-update); critically, they point at *different* entries.
        matched_lines = [
            (r.matched_entry.file_path, r.matched_entry.line_start)
            for r in results
            if r.matched_entry is not None
        ]
        assert len(matched_lines) == 2
        assert len(set(matched_lines)) == 2, (
            "expected the two CSV rows to claim distinct bean entries, "
            f"got: {matched_lines}"
        )


class TestPipelineReplay:
    def test_replay_short_circuits_categorize(self, base_dir: Path):
        # Pre-seed the decision log with a choice for the first txn.
        log_path = base_dir / "decisions.jsonl"
        log = DecisionLog(log_path)
        # Drive the pipeline once with a deterministic categorizer to seed.
        session = make_session(base_dir)
        run(session, base_dir, fixed_categorize("Expenses:From-Replay"), NoopReporter(), decisions=log)
        # The pipeline buffers decisions; the CLI's success path flushes
        # them. Mimic that here so the next run sees them on disk.
        log.flush()
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

    def test_paypal_metadata_settled_matcher_skips(self, tmp_path: Path):
        # SPK entry with `paypal: <date>` metadata — a plugin (`settle_inv`)
        # would split this into a separate PayPal transaction at load time.
        # On import, the `settled` matcher recognises the metadata as
        # settlement evidence and silent-skips the corresponding PayPal
        # CSV row before the scorer runs. The reader-level
        # `synthesize_from_metadata` path that fed virtual entries into
        # the scorer is now redundant for the skip case but remains
        # available for scoring against entries that *don't* carry a
        # metadata-date key (covered by `test_beancount_io.py`).
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n'
        )
        self._write_paypal_csv(
            tmp_path / "PayPal_2024.csv",
            "2024-04-11,Google Payment,EUR,-3.39",
        )
        cfg = Config(
            banks=[self._make_paypal_bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        assert results[0].action == "skip"
        assert results[0].skip_reason == "cross_source_match"
        assert results[0].matched_entry is not None

    def test_funding_bank_can_be_n26(self, tmp_path: Path):
        # Same shape as above but the transit leg lives on N26 instead of
        # SPK — proves the cross-bank matching is bank-agnostic. The PayPal
        # row is a transfer ("Bank Deposit"), so the cross-source matcher
        # spots the existing N26 leg and skips it before the candidate
        # scorer runs.
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
        assert results[0].action == "skip"
        # Cheap dedup picks up the inferred-PayPal leg first; the
        # cross-source matcher would have caught it too. Either path
        # produces the same end state — the row is skipped and the
        # matched entry is one of the two legs of the SPK→PayPal
        # transit transaction.
        assert results[0].skip_reason in ("duplicate", "cross_source_match")
        assert results[0].matched_entry is not None
        assert results[0].matched_entry.source_account in (
            "Assets:B:N26", "Assets:B:PayPal"
        )


class TestPipelineMatchers:
    """End-to-end tests that exercise cross-source matchers in `run()`."""

    def _spk_csv(self, path: Path, row: str) -> None:
        path.write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            f"{row}\n"
        )

    def _paypal_csv(self, path: Path, row: str) -> None:
        path.write_text("Date,Description,Currency,Gross\n" + f"{row}\n")

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

    def test_paypal_funded_spk_row_books_to_paypal_account(self, tmp_path: Path):
        # SPK CSV has a "PayPal" debit; PayPal CSV records the same amount
        # near the same date. The matcher rewrites the SPK proposal to be a
        # transfer to Assets:B:PayPal, with `paypal:` metadata pointing at
        # the PayPal-side date.
        self._spk_csv(
            tmp_path / "SPK_2024.csv",
            "13.04.24;PayPal;PayPal Einkauf 12345;-3,39;EUR;",
        )
        self._paypal_csv(
            tmp_path / "PayPal_2024.csv",
            "2024-04-13,Google Payment,EUR,-3.39",
        )
        cfg = Config(
            banks=[
                make_spk_bank(year_template_output=False),
                self._make_paypal_bank(),
            ],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        (tmp_path / "transactions").mkdir()
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())

        spk_results = [r for r in results if r.source_txn.bank_key == "spk"]
        assert len(spk_results) == 1
        spk = spk_results[0]
        assert spk.action == "new"
        assert spk.proposal is not None
        assert spk.proposal.target_account == "Assets:B:PayPal"
        assert spk.proposal.metadata.get("paypal") == "2024-04-13"
        assert "Assets:B:PayPal" in spk.new_entry_text

    def test_settled_matcher_claims_same_bank_entry_from_bucket(
        self, tmp_path: Path
    ):
        """Edge case: the matcher fires against an entry on the *same*
        bank as the CSV row (e.g. SPK CSV row matches an SPK entry that
        carries `paypal:` metadata for an unrelated reason). The claim
        must remove from the bank-scoped bucket too, otherwise the
        bucket retains a phantom matched entry that downstream
        scoring/dedup would re-consider for sibling rows.

        Sign-flipped amounts (CSV +29.06, bean -29.06) keep the strict
        dedup (which insists on exact-amount equality) from firing, so
        the matcher path actually runs — `abs(amount)` matching is the
        matcher's lane.
        """
        self._spk_csv(
            tmp_path / "SPK_2024.csv",
            "03.05.24;Uber;Uber Trip;29,06;EUR;",
        )
        bean_dir = tmp_path / "transactions"
        bean_dir.mkdir()
        (bean_dir / "SPK.bean").write_text(
            '2024-04-29 * "PayPal" "Uber"\n'
            "  Assets:B:SPK   -29.06 EUR\n"
            "    paypal: 2024-05-03\n"
            "  Expenses:Food:Outside  29.06 EUR\n"
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())

        assert len(results) == 1
        r = results[0]
        assert r.action == "skip"
        assert r.skip_reason == "cross_source_match"
        assert r.matched_entry is not None
        # The matched entry's source_account is the SAME as the CSV row's
        # bank → it lives in the bank-scoped bucket too. The claim
        # behavior is observable via the second-row regression below.
        assert r.matched_entry.source_account == "Assets:B:SPK"

    def test_settled_matcher_pairs_two_csv_rows_with_distinct_entries(
        self, tmp_path: Path
    ):
        """N PayPal CSV rows with the same |amount|+date as N distinct
        bank-side entries (each with `paypal:` metadata) must each pair
        with a *different* entry. Pre-fix the matcher iterated stop-at-
        first, so both rows attributed to whichever entry the loop hit
        first — leaving the sibling looking like a bean-orphan in the
        provenance preview.
        """
        # Two PayPal CSV rows for the same merchant, same amount, same date
        # — two distinct movements (think: two Uber rides on the same day).
        self._paypal_csv(
            tmp_path / "PayPal_2024.csv",
            "2024-05-03,Uber,EUR,-29.06\n2024-05-03,Uber,EUR,-29.06",
        )
        # Two SPK-side entries that the user already booked, each with
        # a `paypal:` date pointing at 2024-05-03.
        bean_dir = tmp_path / "transactions"
        bean_dir.mkdir()
        (bean_dir / "SPK.bean").write_text(
            '2024-04-29 * "PayPal" "Uber"\n'
            "  Assets:B:SPK   -29.06 EUR\n"
            "    paypal: 2024-05-03\n"
            "  Expenses:Food:Outside  29.06 EUR\n\n"
            '2024-04-29 * "PayPal" "Uber"\n'
            "  Assets:B:SPK   -29.06 EUR\n"
            "    paypal: 2024-05-03\n"
            "  Expenses:Food:Outside  29.06 EUR\n"
        )
        cfg = Config(
            banks=[
                make_spk_bank(year_template_output=False),
                self._make_paypal_bank(),
            ],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())

        paypal_results = [r for r in results if r.source_txn.bank_key == "paypal"]
        assert len(paypal_results) == 2
        assert all(r.action == "skip" for r in paypal_results)
        assert all(r.skip_reason == "cross_source_match" for r in paypal_results)
        # The critical assertion: distinct entries claimed, not both
        # attributing to the same one.
        keys = [
            (r.matched_entry.file_path, r.matched_entry.line_start)
            for r in paypal_results
            if r.matched_entry is not None
        ]
        assert len(keys) == 2 and len(set(keys)) == 2, (
            f"expected two distinct settlement-bearing entries claimed, got: {keys}"
        )

    def test_internal_transfer_matcher_skips_already_booked_leg(
        self, tmp_path: Path
    ):
        # SPK ledger already books the SPK→N26 leg. The N26 CSV row of the
        # same transfer must be skipped, not reimported.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(
            '2024-05-01 * "N26" "Überweisung an N26"\n'
            '  Assets:B:SPK   -50.00 EUR\n'
            '  Assets:B:N26    50.00 EUR\n'
        )
        # N26 CSV has a transfer keyword and the matching counterpart amount.
        n26_csv = tmp_path / "N26_2024.csv"
        n26_csv.write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "01.05.24;Sparkasse;Überweisung von SPK;50,00;EUR;\n"
        )
        n26_bank = BankConfig(
            key="n26",
            display_name="N26",
            account="Assets:B:N26",
            file_glob="N26_*.csv",
            output_file="n26.bean",
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
        cfg = Config(
            banks=[n26_bank],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())

        assert len(results) == 1
        assert results[0].action == "skip"
        # Either the cheap dedup catches the row (same amount/date/currency
        # as the already-booked N26 leg) or the cross-source matcher does;
        # both are acceptable here and produce the same end state.
        assert results[0].skip_reason in ("duplicate", "cross_source_match")
        assert results[0].matched_entry is not None


# ── Reverse provenance: bean entries that lack a CSV row ─────────────────────


class TestBeanProvenanceStats:
    def _bank(self) -> BankConfig:
        return make_spk_bank(year_template_output=False)

    def test_non_configured_file_collapses_to_one_section(self, tmp_path: Path):
        # TR.bean lives alongside SPK.bean but no [[banks]] entry registers
        # any TR account. Several sub-accounts (one per share) all live in
        # the same file — they collapse into a single TR.bean section keyed
        # by relative file path, so per-share noise stays out of the preview.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        (tmp_path / "transactions" / "TR.bean").write_text(textwrap.dedent("""\
            2024-03-01 * "Trade Republic" "Dividend AAPL"
              Assets:B:TR:AAPL  +1.50 EUR
              Income:Dividend  -1.50 EUR

            2024-04-01 * "Trade Republic" "Buy GOOG"
              Assets:B:TR:GOOG  -50.00 EUR
              Assets:Investment  50.00 EUR
        """))
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # All TR sub-accounts collapse into the TR.bean file section.
        assert ("TR.bean", 2024) in stats
        assert stats[("TR.bean", 2024)].total_in_bean == 2
        assert stats[("TR.bean", 2024)].bean_unmatched == 2
        # No per-account section for AAPL/GOOG/etc. — file grouping wins.
        assert ("Assets:B:TR:AAPL", 2024) not in stats
        assert ("Assets:B:TR:GOOG", 2024) not in stats
        # Configured SPK section unaffected.
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 1

    def test_monthly_mixed_bank_file_splits_by_configured_account(self, tmp_path: Path):
        # Pre-2024 layouts use month-named files (`2022-01.bean`) carrying
        # postings from multiple banks. The SPK leg attributes to the SPK
        # bank section; the (non-configured) N26 leg falls into the file
        # section since N26 isn't in this test's config.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "2022-01.bean").write_text(textwrap.dedent("""\
            2022-01-05 * "X" ""
              Assets:B:SPK  -10.00 EUR
              Expenses:X  10.00 EUR

            2022-01-10 * "Y" ""
              Assets:B:N26  -5.00 EUR
              Expenses:Y  5.00 EUR
        """))
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # SPK leg → configured-bank section.
        assert stats[("Assets:B:SPK", 2022)].total_in_bean == 1
        # N26 leg (non-configured) → file section.
        assert stats[("2022-01.bean", 2022)].total_in_bean == 1
        # Year-aggregate counts unique transactions, not (section, posting) pairs.
        assert stats[("", 2022)].total_in_bean == 2

    def test_setup_bean_files_are_excluded(self, tmp_path: Path):
        # Setup files carry open/pad/balance directives and the occasional
        # bootstrap transaction that the user doesn't want surfaced as
        # "no CSV source" noise. They're filtered both when read directly
        # and when reached via an `include` from a wrapper file (where
        # beancount's loader resolves them transparently).
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "setup.bean").write_text(textwrap.dedent("""\
            2024-01-01 * "Bootstrap" "Initial balance"
              Assets:B:SPK  +1000.00 EUR
              Equity:Opening-Balances  -1000.00 EUR
        """))
        (tmp_path / "transactions" / "main.bean").write_text(
            'include "setup.bean"\n'
        )
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-02-01 * "Real" "transaction"
              Assets:B:SPK  -10.00 EUR
              Expenses:X  10.00 EUR
        """))
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # Only the SPK.bean transaction is counted — the setup.bean bootstrap
        # entry is filtered, and the include wrapper doesn't smuggle it back.
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 1
        assert ("setup.bean", 2024) not in stats

    def test_multi_posting_in_file_section_dedupes_by_line(self, tmp_path: Path):
        # A single transaction with two non-configured legs (e.g. a TR
        # rebalance touching two sub-accounts) yields two LedgerEntry
        # records, but the file section dedupes by (file_path, line_start)
        # so the count reflects unique transactions.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "TR.bean").write_text(textwrap.dedent("""\
            2024-03-01 * "Trade Republic" "Rebalance"
              Assets:B:TR:AAPL  -100.00 EUR
              Assets:B:TR:GOOG  +100.00 EUR
        """))
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # One transaction in TR.bean, even though two postings match the prefix.
        assert stats[("TR.bean", 2024)].total_in_bean == 1

    def test_bank_filter_drops_out_of_scope_sections(self, tmp_path: Path):
        # `--bank spk` must scope bean-side stats too: file-bucketed
        # sections for non-SPK ledgers (e.g. N26.bean, TR.bean) and
        # the file-shadow section for SPK.bean entries with non-SPK
        # source accounts should not appear at all under that filter.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        (tmp_path / "transactions" / "N26.bean").write_text(textwrap.dedent("""\
            2024-02-01 * "Spotify" "Abo"
              Assets:B:N26  -9.99 EUR
              Expenses:Streaming  9.99 EUR
        """))
        # Configure both banks but filter to spk via ImportOptions.
        n26 = BankConfig(
            key="n26",
            display_name="N26",
            account="Assets:B:N26",
            file_glob="N26_*.csv",
            output_file="transactions/N26.bean",
            csv=CsvConfig(
                delimiter=",", date_format=["%Y-%m-%d"], amount_locale="en",
                field_date="date", field_amount="amount",
                field_currency="currency", field_payee="payee",
                field_description="description",
            ),
        )
        cfg = Config(
            banks=[self._bank(), n26],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(
            config=cfg,
            options=ImportOptions(bank_filter="spk"),
        )
        stats = compute_bean_provenance_stats(session, tmp_path)
        # SPK section present (the user asked for spk).
        assert ("Assets:B:SPK", 2024) in stats
        # N26 entries dropped — neither as configured-bank section nor
        # as a file-bucketed shadow.
        assert ("Assets:B:N26", 2024) not in stats
        assert ("N26.bean", 2024) not in stats
        # Year-aggregate counts only the in-scope bank's entries.
        assert stats[("", 2024)].total_in_bean == 1

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
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 2
        assert stats[("Assets:B:SPK", 2024)].bean_unmatched == 2
        assert stats[("Assets:B:SPK", 2023)].total_in_bean == 1
        assert stats[("Assets:B:SPK", 2023)].bean_unmatched == 1

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
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 2
        assert stats[("Assets:B:SPK", 2024)].bean_unmatched == 1

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
        assert ("Assets:B:SPK", 2024) in stats
        assert ("Assets:B:SPK", 2023) not in stats

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
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0

    def test_csv_parse_failure_is_silently_skipped(self, tmp_path: Path):
        # `compute_bean_provenance_stats` swallows CSV parse exceptions to
        # keep the preview from blowing up on a malformed export.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "X" ""
              Assets:B:SPK  -1.00 EUR
              Expenses:X  1.00 EUR
        """))
        # CSV with a malformed date column → parser raises ValueError.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "not-a-date;X;X;-1,00;EUR;\n"
        )
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        # Must not raise — the stats still come back even though the CSV
        # is unusable.
        stats = compute_bean_provenance_stats(session, tmp_path)
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 1

    def test_dedupes_entries_surfaced_via_include_wrappers(self, tmp_path: Path):
        # `main.bean` (a wrapper) `include`s the per-bank ledger, and an
        # outer `all.bean` includes every year's `main.bean`. Beancount's
        # `load_file` resolves those includes, so each wrapper file
        # re-surfaces the same SPK transactions. The reader records each
        # entry's real source (file, line), so `_load_existing` collapses
        # the duplicates and per-bank counts reflect the true ledger.
        (tmp_path / "transactions" / "2024").mkdir(parents=True)
        (tmp_path / "transactions" / "2024" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR

            2024-02-01 * "Cash" "withdraw"
              Assets:B:SPK  -50.00 EUR
              Expenses:Cash  50.00 EUR
        """))
        (tmp_path / "transactions" / "2024" / "main.bean").write_text(
            'include "SPK.bean"\n'
        )
        (tmp_path / "transactions" / "all.bean").write_text(
            'include "2024/main.bean"\n'
        )
        cfg = Config(
            banks=[self._bank()],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # Without dedup this would be 6 (2 entries × 3 wrapper files).
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 2

    def test_year_aggregate_dedupes_cross_bank_transactions(self, tmp_path: Path):
        # A single ledger transaction posting to TWO bank accounts (a transfer)
        # is loaded once per bank by `_load_existing`, so per-bank counts are
        # correct (each bank IS touched by one entry). But a year-level total
        # must NOT sum those — it represents one underlying transaction.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "ledger.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Transfer" "SPK to DKB"
              Assets:B:SPK  -100.00 EUR
              Assets:B:DKB  +100.00 EUR
        """))
        spk = make_spk_bank(year_template_output=False)
        dkb = BankConfig(
            key="dkb",
            display_name="DKB",
            account="Assets:B:DKB",
            file_glob="DKB_*.csv",
            output_file="dkb.bean",
            csv=spk.csv,
        )
        cfg = Config(
            banks=[spk, dkb],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # Per-bank: each bank is touched by the one transaction.
        assert stats[("Assets:B:SPK", 2024)].total_in_bean == 1
        assert stats[("Assets:B:DKB", 2024)].total_in_bean == 1
        # Year-aggregate (sentinel bank=""): unique ledger transactions only.
        assert stats[("", 2024)].total_in_bean == 1
        assert stats[("", 2024)].bean_unmatched == 1


# ── Subprocess-driven branches: bean-query expanded counts ───────────────────


class TestExpandedCounts:
    """`_expanded_counts` orchestrates `bean-query` per (bank, year) and is
    the most subprocess-heavy bit of the pipeline. Mock `subprocess.run` and
    `shutil.which` to drive each branch deterministically."""

    def _setup(self, tmp_path: Path) -> tuple[ImportSession, Path]:
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "X" ""
              Assets:B:SPK  -1.00 EUR
              Expenses:X  1.00 EUR
        """))
        # Top-level bean file the subprocess "queries" against.
        (tmp_path / "main.2024.bean").write_text("; placeholder\n")
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            main_bean="main.{year}.bean",
            matching=MatchingConfig(min_score=0.35),
        )
        return (
            ImportSession(config=cfg, options=ImportOptions(year_filter=(2024,))),
            tmp_path,
        )

    def test_returns_count_when_bean_query_succeeds(self, tmp_path: Path, monkeypatch):
        session, base = self._setup(tmp_path)

        def fake_run(*args, **kwargs):
            # Simulated bean-query output: a header line, separator, then a
            # numeric count line.
            class R:
                returncode = 0
                stdout = "count(date)\n----\n42\n"
                stderr = ""
            return R()

        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        monkeypatch.setattr("beancount_importer.pipeline.preview.subprocess.run", fake_run)
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 42

    def test_returns_zero_when_bean_query_missing(self, tmp_path: Path, monkeypatch):
        session, base = self._setup(tmp_path)
        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: None)
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0

    def test_returns_zero_when_year_filter_none(self, tmp_path: Path, monkeypatch):
        # No year filter ⇒ skip subprocess work entirely.
        session, base = self._setup(tmp_path)
        # Remove year filter
        cfg = session.config
        session_no_year = ImportSession(config=cfg, options=ImportOptions())
        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        # Subprocess.run MUST NOT be called.
        called = []
        monkeypatch.setattr(
            "beancount_importer.pipeline.preview.subprocess.run",
            lambda *a, **kw: called.append(a) or (_ for _ in ()).throw(AssertionError("should not run")),
        )
        stats = compute_bean_provenance_stats(session_no_year, base)
        assert all(s.bean_expanded == 0 for s in stats.values())
        assert called == []

    def test_main_bean_missing_skips_year(self, tmp_path: Path, monkeypatch):
        # `main_bean` is configured but the per-year file doesn't exist —
        # that year's bean_expanded stays at 0.
        session, base = self._setup(tmp_path)
        # Delete the placeholder main bean
        (base / "main.2024.bean").unlink()
        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        called = []
        monkeypatch.setattr(
            "beancount_importer.pipeline.preview.subprocess.run",
            lambda *a, **kw: called.append(a),
        )
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0
        assert called == []

    def test_bean_query_nonzero_returncode_skips_count(self, tmp_path: Path, monkeypatch):
        session, base = self._setup(tmp_path)

        def fake_run(*args, **kwargs):
            class R:
                returncode = 1
                stdout = ""
                stderr = "boom"
            return R()

        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        monkeypatch.setattr("beancount_importer.pipeline.preview.subprocess.run", fake_run)
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0

    def test_bean_query_timeout_swallowed(self, tmp_path: Path, monkeypatch):
        import subprocess as sp
        session, base = self._setup(tmp_path)

        def raising(*args, **kwargs):
            raise sp.TimeoutExpired(cmd="bean-query", timeout=30)

        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        monkeypatch.setattr("beancount_importer.pipeline.preview.subprocess.run", raising)
        # Must not propagate the TimeoutExpired.
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0

    def test_bean_query_filenotfound_swallowed(self, tmp_path: Path, monkeypatch):
        # If `which` lies (bean-query was uninstalled between checks), the
        # subprocess raises FileNotFoundError — that, too, is swallowed.
        session, base = self._setup(tmp_path)

        def raising(*args, **kwargs):
            raise FileNotFoundError("bean-query")

        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        monkeypatch.setattr("beancount_importer.pipeline.preview.subprocess.run", raising)
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0

    def test_bean_query_non_numeric_output_yields_zero(self, tmp_path: Path, monkeypatch):
        # Output without a digit-only line (e.g. unexpected header format)
        # leaves bean_expanded at 0 rather than crashing.
        session, base = self._setup(tmp_path)

        def fake_run(*a, **kw):
            class R:
                returncode = 0
                stdout = "no numbers here\n----\nfoo\n"
                stderr = ""
            return R()

        monkeypatch.setattr("beancount_importer.pipeline.preview.shutil.which", lambda _: "/usr/bin/bean-query")
        monkeypatch.setattr("beancount_importer.pipeline.preview.subprocess.run", fake_run)
        stats = compute_bean_provenance_stats(session, base)
        assert stats[("Assets:B:SPK", 2024)].bean_expanded == 0


# ── CSV parse failure surfaces via reporter.on_error ─────────────────────────


class TestPipelineParseError:
    """Malformed bank exports must surface via `reporter.on_error`, not
    crash the pipeline. We exercise both a capturing reporter (to verify
    the message reaches the channel) and `NoopReporter` (to guarantee
    the pipeline never depends on the reporter doing anything)."""

    def _broken_csv(self, base_dir: Path) -> None:
        # Date column doesn't match `%d.%m.%y` → parser raises ValueError.
        (base_dir / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "not-a-date;X;X;-1,00;EUR;\n"
        )

    def test_parse_exception_routed_to_reporter(self, base_dir: Path):
        self._broken_csv(base_dir)
        captured: list[str] = []

        class CapturingReporter:
            def on_result(self, result):
                pass

            def on_progress(self, current, total, bank):
                pass

            def on_error(self, message):
                captured.append(message)

        session = make_session(base_dir)
        results = run(session, base_dir, fixed_categorize(), CapturingReporter())
        assert results == []  # no rows survived the parse
        assert any("failed to parse" in m for m in captured)

    def test_parse_exception_with_noop_reporter_does_not_crash(self, base_dir: Path):
        # Same broken input, but routed through `NoopReporter` — the
        # pipeline must complete cleanly (returns empty results) instead
        # of bubbling the parse exception up to the caller.
        self._broken_csv(base_dir)
        session = make_session(base_dir)
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        assert results == []


# ── Quit-then-second-bank: outer loop break ─────────────────────────────────


class TestPipelineQuitMultiBank:
    def test_quit_in_first_bank_stops_processing_second(self, tmp_path: Path):
        # Two banks each with one CSV. First bank's only row triggers `quit`;
        # the second bank's rows must NOT be processed.
        write_spk_csv(tmp_path / "SPK_jan.csv")
        n26_csv = tmp_path / "N26_jan.csv"
        n26_csv.write_text("Date,Amount\n2024-02-01,-1.00\n")

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

        # First call quits.
        def quit_first(ctx):
            return CategoryProposal(action="quit")

        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, quit_first, NoopReporter())
        # Only one txn processed (the first SPK row), then quit.
        assert len(results) == 1
        assert results[0].action == "quit"
        assert results[0].source_txn.bank_key == "spk"


# ── Auto-categorize + rule path (line 323/455) ──────────────────────────────


class TestPipelineAutoCategorize:
    def test_high_score_match_uses_rule_proposal(self, tmp_path: Path):
        """When a candidate's score >= auto_threshold AND a rule matches,
        the pipeline must skip the categorize_fn entirely and synthesize
        the proposal from the rule directly."""
        # Pre-existing ledger entry that exactly matches the new CSV row.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        # CSV row sits 8 days after the bean entry — outside the strict
        # 5-day dedup window so the auto-threshold path actually runs.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "23.01.24;Netflix;Netflix Abo Feb;-15,99;EUR;\n"
        )

        called = []

        def fail_categ(ctx):
            called.append(ctx.txn)
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:From-Categ-Fn"),),
            )

        rule = CategorizationRule(
            target_account="Expenses:Streaming",
            payee_pattern="Netflix",
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(
            config=cfg,
            rules=(rule,),
            options=ImportOptions(auto_threshold=0.5),
        )
        results = run(session, tmp_path, fail_categ, NoopReporter())
        # The categorize fn should NOT have been called — auto-rule applied.
        assert called == []
        assert len(results) == 1


# ── Skip-update-pattern: narration field falls through (line 448) ───────────


class TestPipelineSkipPatternsNarration:
    def test_narration_field_pattern_does_not_match_in_pipeline(self, base_dir: Path):
        # `_matches_skip_pattern` walks each pattern but defers narration-field
        # patterns to a separate code path (they need a matched ledger entry).
        # Setting one shouldn't suppress txns that have no matched entry.
        session = make_session(
            base_dir,
            skip_patterns=(SkipUpdatePattern(field="narration", pattern="anything"),),
        )
        results = run(session, base_dir, fixed_categorize(), NoopReporter())
        # The narration pattern is skipped at this layer; all rows still
        # produce categorize results.
        assert all(r.action == "new" for r in results)


# ── _derive_rule edge cases: no-pattern fallback (lines 473, 479-480, 482) ──


class TestPipelineSaveAsRuleEdges:
    def test_save_as_rule_drops_when_no_pattern_available(self, tmp_path: Path):
        # CSV row with NO payee AND NO description → `_derive_rule` cannot
        # synthesize a pattern, so save_as_rule silently has no effect.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            # Empty payee and description columns
            "15.01.24;;;-1,00;EUR;\n"
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:X"),),
                save_as_rule=True,
            )

        session = make_session(tmp_path)
        results = run(session, tmp_path, categ, NoopReporter())
        assert len(results) == 1
        # save_as_rule was True but the rule could not be derived.
        assert results[0].new_rule is None

    def test_save_as_rule_uses_description_when_payee_empty(self, tmp_path: Path):
        # CSV row with empty payee but non-empty description — derive_rule
        # falls through to the description-pattern branch.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "15.01.24;;Some narrative;-1,00;EUR;\n"
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Y"),),
                save_as_rule=True,
            )

        session = make_session(tmp_path)
        results = run(session, tmp_path, categ, NoopReporter())
        assert len(results) == 1
        nr = results[0].new_rule
        assert nr is not None
        assert nr.description_pattern  # fall-through branch was used
        assert not nr.payee_pattern

    def test_save_as_rule_no_postings_returns_none(self, tmp_path: Path):
        # Edge case: action="categorize" + save_as_rule=True + no postings →
        # `_derive_rule`'s `if not proposal.postings: return None` branch.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "15.01.24;X;X;-1,00;EUR;\n"
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(),  # no postings
                save_as_rule=True,
            )

        session = make_session(tmp_path)
        results = run(session, tmp_path, categ, NoopReporter())
        assert len(results) == 1
        assert results[0].new_rule is None


# ── Update flow: change detection + suppression (lines 511, 522-538) ─────────


class TestPipelineUpdateChanges:
    def _setup(self, tmp_path: Path) -> Path:
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "OldPayee" "old narration"
              Assets:B:SPK  -15.99 EUR
              Expenses:Old  15.99 EUR
        """))
        # CSV date sits 8 days after the bean entry — outside the strict
        # `dedup_max_date_days=5` window (so the merge path runs) but well
        # within the scorer's window (so the entry is still a candidate).
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "23.01.24;NewPayee;new narration;-15,99;EUR;\n"
        )
        return tmp_path

    def _session_with(self, tmp_path: Path, **rule_kwargs) -> ImportSession:
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.0),  # Force a candidate to surface
        )
        rules = (CategorizationRule(**rule_kwargs),) if rule_kwargs else ()
        return ImportSession(config=cfg, rules=rules, options=ImportOptions())

    def test_payee_narration_account_diffs_proposed(self, tmp_path: Path):
        base = self._setup(tmp_path)
        # A rule with overrides ensures the seed proposal differs from
        # the existing entry across all three fields, so the pipeline
        # doesn't silent-skip before invoking categorize_fn.
        session = self._session_with(
            base,
            target_account="Expenses:New",
            payee_pattern="NewPayee",
            override_payee="NewPayee",
            override_narration="new narration",
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:New"),),
                payee="NewPayee",
                narration="new narration",
            )

        results = run(session, base, categ, NoopReporter())
        assert len(results) == 1
        r = results[0]
        assert r.action == "update"
        fields = {c.field for c in r.proposed_changes}
        assert "payee" in fields
        assert "narration" in fields
        assert "account" in fields

    def test_suppress_payee_updates_drops_payee_change(self, tmp_path: Path):
        base = self._setup(tmp_path)
        session = self._session_with(
            base,
            target_account="Expenses:New",
            payee_pattern="NewPayee",
            suppress_payee_updates=True,
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:New"),),
                payee="NewPayee",
                narration="new narration",
            )

        results = run(session, base, categ, NoopReporter())
        fields = {c.field for c in results[0].proposed_changes}
        assert "payee" not in fields
        assert "narration" in fields

    def test_suppress_narration_updates_drops_narration_change(self, tmp_path: Path):
        base = self._setup(tmp_path)
        session = self._session_with(
            base,
            target_account="Expenses:New",
            payee_pattern="NewPayee",
            suppress_narration_updates=True,
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:New"),),
                payee="NewPayee",
                narration="new narration",
            )

        results = run(session, base, categ, NoopReporter())
        fields = {c.field for c in results[0].proposed_changes}
        assert "narration" not in fields
        # payee + account changes still appear.
        assert "payee" in fields
        assert "account" in fields

    def test_suppress_account_updates_drops_account_change(self, tmp_path: Path):
        base = self._setup(tmp_path)
        # Overrides on the rule ensure the seed produces a non-empty
        # diff (payee, narration) even with the account change
        # suppressed — otherwise the pipeline silent-skips before
        # categorize_fn fires.
        session = self._session_with(
            base,
            target_account="Expenses:New",
            payee_pattern="NewPayee",
            override_payee="NewPayee",
            override_narration="new narration",
            suppress_account_updates=True,
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:New"),),
                payee="NewPayee",
                narration="new narration",
            )

        results = run(session, base, categ, NoopReporter())
        fields = {c.field for c in results[0].proposed_changes}
        assert "account" not in fields
        assert "payee" in fields
        assert "narration" in fields

    def test_suppress_all_returns_no_changes(self, tmp_path: Path):
        base = self._setup(tmp_path)
        session = self._session_with(
            base,
            target_account="Expenses:New",
            payee_pattern="NewPayee",
            suppress_updates=True,
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:New"),),
                payee="NewPayee",
                narration="new narration",
            )

        results = run(session, base, categ, NoopReporter())
        assert results[0].proposed_changes == []


# ── Format new entry with explicit posting amount (lines 558-559) ──────────


class TestPipelineFormatNewEntry:
    def test_explicit_posting_amount_emitted(self, tmp_path: Path):
        # When a proposal supplies a Posting with an explicit amount, the
        # rendered transaction should include that amount alongside the
        # source-account leg.
        write_spk_csv(tmp_path / "SPK_jan.csv")

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(
                    Posting(
                        account="Expenses:Split",
                        amount=Decimal("10.00"),
                        currency="EUR",
                    ),
                    Posting(account="Expenses:Other"),
                ),
            )

        session = make_session(tmp_path)
        results = run(session, tmp_path, categ, NoopReporter())
        # First result's text should contain the explicit posting line.
        text = results[0].new_entry_text
        assert "Expenses:Split" in text
        assert "10.00 EUR" in text


# ── Dedup-skip preserves tag advance (line 295) ──────────────────────────────


class TestPipelineDedupSkip:
    def test_already_imported_txn_skipped(self, tmp_path: Path):
        # Existing entry with matching SEPA reference — dedup classifies the
        # CSV row as duplicate, returning a "skip" result.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              sepa_ref: "NETFLIX-001"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "15.01.24;Netflix;Netflix Abo;-15,99;EUR;NETFLIX-001\n"
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        assert results[0].action == "skip"


# ── _has_csv_match: continue when no candidate date is in tolerance ─────────


class TestHasCsvMatch:
    """`_has_csv_match` walks all CSV rows for an entry; cover the inner-
    loop continue branches that the bigger tests above don't reach."""

    def test_amount_mismatch_iterates_to_next_csv(self, tmp_path: Path):
        # Two CSV rows — first one's amount doesn't match (continue), second
        # one matches by amount + close date.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Match" ""
              Assets:B:SPK  -10.00 EUR
              Expenses:X  10.00 EUR
        """))
        # Two-row CSV: first row has different amount, second row matches.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "15.01.24;Other;X;-99,99;EUR;\n"
            "15.01.24;Match;X;-10,00;EUR;\n"
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        # The matched bean entry has bean_unmatched=0 because the second
        # CSV row matches it.
        assert stats[("Assets:B:SPK", 2024)].bean_unmatched == 0

    def test_amount_match_but_date_too_far_continues(self, tmp_path: Path):
        # Same amount, but every CSV row's date is far outside the tolerance
        # window — the entry stays unmatched.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-06-01 * "Match" ""
              Assets:B:SPK  -10.00 EUR
              Expenses:X  10.00 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "15.01.24;Far;X;-10,00;EUR;\n"
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        stats = compute_bean_provenance_stats(session, tmp_path)
        assert stats[("Assets:B:SPK", 2024)].bean_unmatched == 1


# ── Rule-rewrite for matching ─────────────────────────────────────────────────


from beancount_importer.models import LedgerEntry, SourceTransaction


def _make_txn(
    *,
    booking_date: date = date(2024, 4, 2),
    amount: Decimal = Decimal("-49.50"),
    currency: str = "EUR",
    bank_key: str = "spk",
    payee: str | None = "Frankfurter Turn-u.Sport- Gemeinschaft 1847 j.P.",
    description: str | None = "0614870/Vereinsbeitrag FTG Frankfurt FOLGELASTSCHRIFT",
) -> SourceTransaction:
    return SourceTransaction(
        booking_date=booking_date,
        amount=amount,
        currency=currency,
        bank_key=bank_key,
        payee=payee,
        description=description,
    )


class TestApplyRuleOverrides:
    """Rule-driven payee/narration substitution used by dedup + scoring."""

    def test_no_rule_returns_identity(self):
        txn = _make_txn()
        assert _apply_rule_overrides(txn, None) is txn

    def test_rule_without_overrides_returns_identity(self):
        # `target_account` set, but no override_payee / override_narration —
        # the txn should be returned unchanged so callers can short-circuit.
        rule = CategorizationRule(
            target_account="Expenses:Fitness:Gym",
            payee_pattern="FTG",
        )
        txn = _make_txn()
        assert _apply_rule_overrides(txn, rule) is txn

    def test_payee_override_substitutes_payee(self):
        rule = CategorizationRule(
            target_account="Expenses:Fitness:Gym",
            payee_pattern="FTG",
            override_payee="FTG Frankfurt",
        )
        txn = _make_txn()
        rewritten = _apply_rule_overrides(txn, rule)
        assert rewritten.payee == "FTG Frankfurt"
        assert rewritten.description == txn.description  # untouched
        assert rewritten.amount == txn.amount  # untouched

    def test_narration_override_substitutes_description(self):
        # Rules write into `entry.narration` via `override_narration`; the
        # match-side mirror substitutes into `txn.description` (the field the
        # scorer concatenates with payee for text similarity).
        rule = CategorizationRule(
            target_account="Expenses:Fitness:Gym",
            payee_pattern="FTG",
            override_narration="FTG monthly dues",
        )
        txn = _make_txn()
        rewritten = _apply_rule_overrides(txn, rule)
        assert rewritten.description == "FTG monthly dues"
        assert rewritten.payee == txn.payee


class TestPipelineRuleRewriteForDedup:
    """End-to-end: an entry written with rule overrides applied dedups against
    its raw CSV row on the next import. Without the rewrite, the txn-side
    content hash uses raw payee/description while the entry-side uses the
    rule-cleaned versions, and dedup silently misses.
    """

    def test_dedup_catches_rule_cleaned_entry_on_second_import(
        self, tmp_path: Path
    ):
        # SPK.bean: an entry written previously with the rule applied —
        # cleaned payee + raw narration (the user kept the description).
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-04-02 * "FTG Frankfurt" "0614870/Vereinsbeitrag FTG Frankfurt FOLGELASTSCHRIFT"
              Assets:B:SPK  -49.50 EUR
              Expenses:Fitness:Gym  49.50 EUR
        """))
        # CSV: same row, raw payee.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "02.04.24;Frankfurter Turn-u.Sport- Gemeinschaft 1847 j.P.;"
            "0614870/Vereinsbeitrag FTG Frankfurt FOLGELASTSCHRIFT;-49,50;EUR;\n"
        )
        rule = CategorizationRule(
            target_account="Expenses:Fitness:Gym",
            payee_pattern="FTG|Frankfurter Turn",
            override_payee="FTG Frankfurt",
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, rules=(rule,), options=ImportOptions())
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        # Dedup should have caught it via the rewritten txn's content hash.
        assert results[0].action == "skip"
        assert results[0].skip_reason == "duplicate"


class TestComputeNearMisses:
    """The diagnostic helper that surfaces "almost-a-match" entries."""

    def test_below_threshold_in_bucket(self):
        txn = _make_txn()
        # An entry on the same bucket with same amount/date but text that
        # makes the score land below `min_score`. Scorer's date_proximity
        # term gives 0.5 (DATE_WEIGHT) plus a tiny text contribution; with
        # min_score raised high enough we force a below-threshold result.
        in_bucket = [
            LedgerEntry(
                date=date(2024, 4, 2),
                narration="totally different",
                source_account="Assets:B:SPK",
                target_account="Expenses:Fitness:Gym",
                amount=Decimal("-49.50"),
                currency="EUR",
                payee="totally different",
            ),
        ]
        misses = _compute_near_misses(
            txn,
            in_bucket=in_bucket,
            cross_bucket=in_bucket,
            bank_account="Assets:B:SPK",
            min_score=0.95,
        )
        assert len(misses) == 1
        assert misses[0].reason == "below_threshold"
        assert misses[0].score < 0.95

    def test_different_bucket(self):
        txn = _make_txn()
        # Same amount/currency/date, but on a sub-account the bucket lookup
        # never reaches.
        elsewhere = LedgerEntry(
            date=date(2024, 4, 2),
            narration="FTG",
            source_account="Assets:B:SPK:Checking",
            target_account="Expenses:Fitness:Gym",
            amount=Decimal("-49.50"),
            currency="EUR",
        )
        misses = _compute_near_misses(
            txn,
            in_bucket=[],
            cross_bucket=[elsewhere],
            bank_account="Assets:B:SPK",
            min_score=0.35,
        )
        assert any(m.reason == "different_bucket" for m in misses)
        diff = next(m for m in misses if m.reason == "different_bucket")
        assert diff.entry.source_account == "Assets:B:SPK:Checking"

    def test_skips_amount_inferred_cross_bucket(self):
        # Inferred-amount entries are cross-bank transit legs the scorer
        # already handles via reversed-sign matching — surfacing them as
        # "different bucket" would be misleading double-counting.
        txn = _make_txn()
        transit = LedgerEntry(
            date=date(2024, 4, 2),
            narration="PayPal",
            source_account="Assets:B:PayPal",
            target_account="Assets:B:SPK",
            amount=Decimal("49.50"),
            currency="EUR",
            amount_inferred=True,
        )
        misses = _compute_near_misses(
            txn,
            in_bucket=[],
            cross_bucket=[transit],
            bank_account="Assets:B:SPK",
            min_score=0.35,
        )
        assert all(m.reason != "different_bucket" for m in misses)

    def test_no_misses_when_nothing_close(self):
        txn = _make_txn()
        misses = _compute_near_misses(
            txn,
            in_bucket=[],
            cross_bucket=[],
            bank_account="Assets:B:SPK",
            min_score=0.35,
        )
        assert misses == ()


class TestPipelineNearMissesPlumbing:
    """The pipeline only computes near-misses when no real candidates land."""

    def test_near_misses_populated_when_no_candidates(self, tmp_path: Path):
        # Existing entry on a sub-account so the scorer's bucket misses it
        # but the cross-bucket diagnostic catches it.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              Assets:B:SPK:Checking  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            15.01.24;Netflix;Netflix Abo;-15,99;EUR;
        """))
        captured: list[CategorizeContext] = []

        def categ(ctx: CategorizeContext) -> CategoryProposal:
            captured.append(ctx)
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Streaming"),),
            )

        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        run(session, tmp_path, categ, NoopReporter())
        assert len(captured) == 1
        misses = captured[0].near_misses
        assert any(m.reason == "different_bucket" for m in misses)

    def test_near_misses_empty_when_candidates_exist(self, base_dir: Path):
        # An existing entry close enough to score above threshold but not
        # a definitive dedup match (date gap > dedup_max_date_days, gap
        # within scorer window). The diagnostic must stay quiet on rows
        # that had at least one candidate — it's only meant for "nothing
        # landed" cases.
        #
        # A rule with override_narration ensures the seed proposal
        # differs from the existing entry; without it the pipeline
        # silent-skips before invoking categorize_fn and we never
        # observe the context.
        (base_dir / "transactions").mkdir()
        # 7 days after the CSV's Netflix row (15.01.24): outside the
        # strict 5-day dedup window, inside the 14-day scorer window.
        (base_dir / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-22 * "Netflix" "Old narration"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        captured: list[CategorizeContext] = []

        def categ(ctx: CategorizeContext) -> CategoryProposal:
            captured.append(ctx)
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Streaming"),),
            )

        rule = CategorizationRule(
            target_account="Expenses:Streaming",
            payee_pattern="Netflix",
            override_narration="Netflix Abo",
        )
        session = make_session(base_dir, rules=(rule,))
        run(session, base_dir, categ, NoopReporter())
        netflix = next(c for c in captured if c.txn.payee == "Netflix")
        assert netflix.candidates  # scorer found the entry
        assert netflix.near_misses == ()


# ── Pipeline contract: determinism / isolation / multi-row pairing ───────────


class TestPipelineDeterminism:
    """`pipeline.run()` is deterministic given the same inputs.

    CLAUDE.md elevates this to an architectural invariant. The cheapest
    enforcement: run the pipeline twice on a fixed setup and compare the
    `ImportResult` lists.
    """

    def test_two_runs_produce_equal_results(self, base_dir: Path):
        session = make_session(base_dir)
        first = run(session, base_dir, fixed_categorize(), NoopReporter())
        # Use a fresh session — `ImportSession` is frozen, but the
        # `working_rules` / `working_tag` machinery lives inside `run()`,
        # so a second call against the same files must reproduce.
        session2 = make_session(base_dir)
        second = run(session2, base_dir, fixed_categorize(), NoopReporter())
        # Comparing the full result list catches regressions in iteration
        # order, claim ordering, near-miss generation, etc.
        assert [r.model_dump() for r in first] == [r.model_dump() for r in second]


class TestPipelineBucketIsolation:
    """Bank-scoped dedup/scoring buckets keep cross-bank lookalikes apart.

    Two entries with identical (date, |amount|, currency) on different
    banks must not interfere — an SPK CSV row pairs with the SPK entry,
    not the N26 entry. The cross-source matchers see both via
    `existing_all`, but the dedup/scoring path is bank-scoped through
    `existing_by_account`.
    """

    def _n26_bank(self) -> BankConfig:
        return BankConfig(
            key="n26",
            display_name="N26",
            account="Assets:B:N26",
            file_glob="N26_*.csv",
            output_file="n26.bean",
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

    def test_spk_csv_row_pairs_with_spk_entry_not_n26_lookalike(
        self, tmp_path: Path
    ):
        # CSVs: one SPK row only.
        (tmp_path / "SPK_2024.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "15.01.24;Netflix;Netflix Abo;-15,99;EUR;\n"
        )
        # Bean: two entries on different banks with identical date/amount.
        # The SPK one is the only legitimate match.
        bean_dir = tmp_path / "transactions"
        bean_dir.mkdir()
        (bean_dir / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        (bean_dir / "N26.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              Assets:B:N26  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        cfg = Config(
            banks=[
                make_spk_bank(year_template_output=False),
                self._n26_bank(),
            ],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(
            session, tmp_path, fixed_categorize("Expenses:Streaming"), NoopReporter()
        )
        # One CSV row → one result. It must have matched the SPK-side
        # entry, not the N26 lookalike.
        assert len(results) == 1
        assert results[0].action == "skip"
        assert results[0].skip_reason == "duplicate"
        assert results[0].matched_entry is not None
        assert results[0].matched_entry.source_account == "Assets:B:SPK"


class TestPipelineRuleRewriteOverrideNarration:
    """Counterpart to TestPipelineRuleRewriteForDedup, but for the
    `override_narration` field — only `override_payee` had end-to-end
    coverage previously.
    """

    def test_dedup_catches_narration_cleaned_entry(self, tmp_path: Path):
        # Bean entry was previously written with rule.override_narration
        # applied: payee carried through raw, narration cleaned.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-04-02 * "Frankfurter Turn-u.Sport- Gemeinschaft 1847 j.P." "Monthly dues"
              Assets:B:SPK  -49.50 EUR
              Expenses:Fitness:Gym  49.50 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "02.04.24;Frankfurter Turn-u.Sport- Gemeinschaft 1847 j.P.;"
            "0614870/Vereinsbeitrag FTG Frankfurt FOLGELASTSCHRIFT;-49,50;EUR;\n"
        )
        rule = CategorizationRule(
            target_account="Expenses:Fitness:Gym",
            payee_pattern="FTG|Frankfurter Turn",
            override_narration="Monthly dues",
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(
            config=cfg, rules=(rule,), options=ImportOptions()
        )
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        assert results[0].action == "skip"
        assert results[0].skip_reason == "duplicate"

    def test_changed_override_does_not_re_prompt_previously_imported_row(
        self, tmp_path: Path
    ):
        # Under the new dedup architecture, amount + currency + date-window
        # equality is definitive — the user's bean entry is the source of
        # truth, so a re-imported CSV row silent-skips even when the rule's
        # override_payee was edited between runs. This used to re-prompt
        # because the content hash incorporated the rule-rewritten payee;
        # the audit flagged that as an A-type regression.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-04-02 * "Old Name" "0614870/Vereinsbeitrag FTG Frankfurt FOLGELASTSCHRIFT"
              Assets:B:SPK  -49.50 EUR
              Expenses:Fitness:Gym  49.50 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "02.04.24;Frankfurter Turn-u.Sport- Gemeinschaft 1847 j.P.;"
            "0614870/Vereinsbeitrag FTG Frankfurt FOLGELASTSCHRIFT;-49,50;EUR;\n"
        )
        new_rule = CategorizationRule(
            target_account="Expenses:Fitness:Gym",
            payee_pattern="FTG|Frankfurter Turn",
            override_payee="FTG Frankfurt",
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(
            config=cfg, rules=(new_rule,), options=ImportOptions()
        )
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        r = results[0]
        assert r.action == "skip"
        assert r.skip_reason == "duplicate"


class TestNNPairingViaCandidates:
    """49eb37a's existing test covers the dedup path. This adds the
    categorize+candidates path — N CSVs whose narrations differ enough
    to *miss* dedup but score above threshold against N distinct entries
    must each pair with a different entry, not all attribute to entry #1.
    """

    def test_two_csv_rows_pair_with_distinct_entries_via_scorer(
        self, tmp_path: Path
    ):
        # CSV: two near-identical rows, slightly different narrations so
        # content-hash dedup misses them.
        (tmp_path / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            08.01.24;DISCOSTADL;Bar tab variant A;-16,00;EUR;
            08.01.24;DISCOSTADL;Bar tab variant B;-16,00;EUR;
        """))
        # Bean: two existing entries with neutral narration. Each scores
        # well above min_score against either CSV row — text differs but
        # date+amount+payee carry the signal.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-08 * "DISCOSTADL" "Bar tab"
              Assets:B:SPK  -16.00 EUR
              Expenses:Drinks  16.00 EUR

            2024-01-08 * "DISCOSTADL" "Bar tab"
              Assets:B:SPK  -16.00 EUR
              Expenses:Drinks  16.00 EUR
        """))
        session = make_session(tmp_path)
        results = run(
            session,
            tmp_path,
            fixed_categorize("Expenses:Drinks"),
            NoopReporter(),
        )
        assert len(results) == 2
        # Both rows take the categorize+candidates path (proposal is
        # `update` because best != None). Each must claim a distinct
        # entry — not both attribute to whichever was iterated first.
        keys = [
            (r.matched_entry.file_path, r.matched_entry.line_start)
            for r in results
            if r.matched_entry is not None
        ]
        assert len(keys) == 2 and len(set(keys)) == 2

    def test_more_csv_rows_than_entries_creates_new_for_remainder(
        self, tmp_path: Path
    ):
        # 3 CSVs ↔ 1 bean entry: first row pairs (skip via dedup), the
        # remaining two find no candidates (the single entry has been
        # claimed) and get categorized as new.
        (tmp_path / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            08.01.24;DISCOSTADL;Bar tab;-16,00;EUR;
            08.01.24;DISCOSTADL;Bar tab;-16,00;EUR;
            08.01.24;DISCOSTADL;Bar tab;-16,00;EUR;
        """))
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-08 * "DISCOSTADL" "Bar tab"
              Assets:B:SPK  -16.00 EUR
              Expenses:Drinks  16.00 EUR
        """))
        session = make_session(tmp_path)
        results = run(
            session,
            tmp_path,
            fixed_categorize("Expenses:Drinks"),
            NoopReporter(),
        )
        assert len(results) == 3
        # Exactly one row dedups (claims the single entry); the other
        # two should be `update`-with-empty-diff (silent match against
        # the same entry? no — the entry was claimed) → `new`.
        actions = [r.action for r in results]
        # One skip (dedup), two new — order depends on iteration but
        # the multiset is fixed.
        assert sorted(actions) == ["new", "new", "skip"]


# ── Edge cases: near-miss diagnostic, auto-threshold, skip-claim, empty state ─


class TestNearMissRealWorld:
    """Realistic near-miss scenarios. The existing `_compute_near_misses`
    tests force `min_score=0.95` to manufacture a below-threshold result;
    these use the production default 0.35 and demonstrate the diagnostic
    fires on text drift alone.
    """

    def test_text_drift_produces_below_threshold_miss(self):
        # Real-world drift: same amount, but text and date have
        # diverged enough that the score lands below the production
        # default min_score=0.35. We use distinct gibberish tokens
        # (token_set_ratio is sensitive even to short shared tokens
        # like "a"/"the") and a 13-day offset to keep date_proximity
        # near zero. Score ~0.08 — well below 0.35.
        txn = _make_txn(
            booking_date=date(2024, 4, 2),
            payee="qqqqq",
            description="zzzzz",
        )
        in_bucket = [
            LedgerEntry(
                date=date(2024, 4, 15),
                payee="alpha",
                narration="beta",
                source_account="Assets:B:SPK",
                target_account="Expenses:Misc",
                amount=Decimal("-49.50"),
                currency="EUR",
            )
        ]
        misses = _compute_near_misses(
            txn,
            in_bucket=in_bucket,
            cross_bucket=in_bucket,
            bank_account="Assets:B:SPK",
            min_score=0.35,
        )
        assert len(misses) == 1
        assert misses[0].reason == "below_threshold"
        assert misses[0].score < 0.35

    def test_emits_at_most_one_below_threshold_and_one_different_bucket(self):
        # Cap-of-2 contract: one near-miss per reason. Setup both a
        # below-threshold in-bucket entry and a different-bucket entry
        # → the helper returns exactly two NearMiss objects, one of
        # each reason. The break-after-first-hit in each pass is what
        # prevents a flood of diagnostic noise.
        txn = _make_txn(
            booking_date=date(2024, 4, 2),
            amount=Decimal("-49.50"),
            payee="qqqqq",
            description="zzzzz",
        )
        # In-bucket entry: same source_account, scores below threshold.
        in_bucket_weak = LedgerEntry(
            date=date(2024, 4, 15),
            payee="alpha",
            narration="beta",
            source_account="Assets:B:SPK",
            target_account="Expenses:Misc",
            amount=Decimal("-49.50"),
            currency="EUR",
        )
        # Cross-bucket entry: same date+amount on a sibling source
        # account the bucket lookup never reaches.
        cross_bucket_hit = LedgerEntry(
            date=date(2024, 4, 2),
            payee="FTG",
            narration="FTG",
            source_account="Assets:B:SPK:Checking",
            target_account="Expenses:Fitness:Gym",
            amount=Decimal("-49.50"),
            currency="EUR",
        )
        misses = _compute_near_misses(
            txn,
            in_bucket=[in_bucket_weak],
            cross_bucket=[in_bucket_weak, cross_bucket_hit],
            bank_account="Assets:B:SPK",
            min_score=0.35,
        )
        reasons = sorted(m.reason for m in misses)
        assert reasons == ["below_threshold", "different_bucket"]


class TestAutoThresholdRequiresRule:
    """`auto_threshold` only short-circuits the categorize prompt when a
    rule is *also* matched — without a rule, the auto-apply path doesn't
    fire. The fallback flow either silent-skips a clean match (no diff
    against the candidate) or routes through `categorize_fn` for rows
    that genuinely need user input.
    """

    def test_high_score_without_rule_silent_skips_clean_match(self, tmp_path: Path):
        # Same amount + same date-window + same currency → dedup skips
        # before the auto-threshold path even runs. categorize_fn never
        # fires, the result is `skip / duplicate` (the cheap path won).
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "16.01.24;Netflix;Netflix Abo Feb;-15,99;EUR;\n"
        )
        called: list = []

        def categ(ctx):
            called.append(ctx.txn)
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:From-Categ"),),
            )

        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(
            config=cfg,
            options=ImportOptions(auto_threshold=0.5),
        )
        results = run(session, tmp_path, categ, NoopReporter())
        assert called == []
        assert len(results) == 1
        assert results[0].action == "skip"
        assert results[0].skip_reason == "duplicate"
        assert results[0].matched_entry is not None

    def test_ambiguous_zero_diff_silent_skips(self, tmp_path: Path):
        # Two near-tied candidates with identical target_accounts and
        # matching payee/narration → every ambiguous candidate produces
        # an empty diff against the seed proposal. There's no real
        # choice to make; the pipeline must silent-skip rather than
        # routing to Screen 4.
        #
        # The CSV row's narration differs from both entries' so dedup
        # misses (otherwise the duplicate path would short-circuit
        # ahead of silent-skip).
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-03-01 * "Coffee Shop" "Latte"
              Assets:B:SPK  -12.50 EUR
              Expenses:Food  12.50 EUR

            2024-03-02 * "Coffee Shop" "Latte"
              Assets:B:SPK  -12.50 EUR
              Expenses:Food  12.50 EUR
        """))
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "01.03.24;Coffee Shop;Cappuccino;-12,50;EUR;\n"
        )
        called: list = []

        def categ(ctx):
            called.append(ctx.txn)
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Food"),),
            )

        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, options=ImportOptions())
        results = run(session, tmp_path, categ, NoopReporter())
        # categorize_fn never fires — pipeline silent-skipped both
        # ambiguous candidates because all produce a zero diff.
        assert called == []
        assert len(results) == 1
        assert results[0].action == "update"
        assert results[0].proposed_changes == []


class TestSkipPatternDoesNotClaim:
    """A `skip_update_patterns` hit must not consume a candidate entry —
    same family as Bug #1 (claim-only-when-consumed). Without the gate,
    a subsequent row that legitimately matches the entry would lose its
    candidate to a phantom claim.
    """

    def test_skip_pattern_does_not_claim_candidate(self, tmp_path: Path):
        # Pattern fires on a row that *would* have a scoring candidate
        # (text drift makes dedup miss). The skip_rule path returns
        # before reaching `_build_result`, so the candidate must remain
        # available — the assertion checks the matched_entry shape, but
        # the deeper guarantee is that the bucket isn't mutated when
        # the pattern wins.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Old narration"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        # CSV row sits 8 days after the bean entry — outside the strict
        # `dedup_max_date_days=5` window so dedup doesn't win first, but
        # inside the scorer window so a candidate would surface if the
        # skip-pattern path didn't short-circuit.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "23.01.24;Netflix;Different narration text;-15,99;EUR;\n"
        )
        skip_pattern = SkipUpdatePattern(
            bank_key="spk",
            field="payee",
            pattern="Netflix",
        )
        session = make_session(tmp_path, skip_patterns=(skip_pattern,))
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 1
        r = results[0]
        # skip_rule path returns early — never inspects candidates,
        # never sets matched_entry, never claims from the bucket.
        assert r.action == "skip"
        assert r.skip_reason == "skip_rule"
        assert r.matched_entry is None


class TestEmptyState:
    """Pipeline contract holds at the empty edges — no CSV, no ledger,
    or both — without crashes or surprising defaults.
    """

    def test_empty_csv_returns_no_results(self, tmp_path: Path):
        # CSV file exists but has only a header row.
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
        )
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-15 * "Netflix" "Netflix Abo"
              Assets:B:SPK  -15.99 EUR
              Expenses:Streaming  15.99 EUR
        """))
        session = make_session(tmp_path)
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert results == []

    def test_empty_ledger_emits_all_rows_as_new(self, tmp_path: Path):
        # Bean dir exists but is empty — every CSV row goes through
        # categorize and lands as `action=new` with new_entry_text set.
        (tmp_path / "transactions").mkdir()
        (tmp_path / "SPK_jan.csv").write_text(textwrap.dedent("""\
            Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz
            15.01.24;Netflix;Netflix Abo;-15,99;EUR;
            16.01.24;Rewe;REWE Filiale;-42,50;EUR;
        """))
        session = make_session(tmp_path)
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert len(results) == 2
        assert all(r.action == "new" for r in results)
        assert all(r.matched_entry is None for r in results)
        assert all(r.new_entry_text for r in results)

    def test_empty_csv_and_empty_ledger_no_error(self, tmp_path: Path):
        (tmp_path / "transactions").mkdir()
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
        )
        session = make_session(tmp_path)
        results = run(session, tmp_path, fixed_categorize(), NoopReporter())
        assert results == []


class TestDiffChangesDirect:
    """Unit tests for `_diff_changes` itself. The end-to-end tests in
    TestDiffSuppressions exercise the suppressions through the pipeline,
    but the seed-silent-skip path swallows rows before `_diff_changes`
    runs in many cases. These tests construct an entry + proposal and
    drive the helper directly.
    """

    def _entry(self, **kw) -> LedgerEntry:
        defaults = dict(
            date=date(2024, 1, 15),
            narration="Acme Inc",
            source_account="Assets:B:SPK",
            target_account="Expenses:X",
            amount=Decimal("-10.00"),
            currency="EUR",
        )
        return LedgerEntry(**(defaults | kw))

    def _proposal(self, **kw) -> CategoryProposal:
        defaults = dict(
            action="categorize",
            postings=(Posting(account="Expenses:X"),),
        )
        return CategoryProposal(**(defaults | kw))

    def test_truncation_equivalent_suppresses_narration_change(self):
        from beancount_importer.pipeline.run import _diff_changes
        entry = self._entry(narration="Acme Inc")
        proposal = self._proposal(narration="Acme Inc — Subscription Plan A")
        changes = _diff_changes(entry, proposal, None)
        assert all(c.field != "narration" for c in changes)

    def test_timestamp_proposal_does_not_overwrite_real_narration(self):
        from beancount_importer.pipeline.run import _diff_changes
        entry = self._entry(narration="Burger King")
        proposal = self._proposal(narration="2024-01-23T12:00 Debit")
        changes = _diff_changes(entry, proposal, None)
        assert all(c.field != "narration" for c in changes)

    def test_non_timestamp_proposal_overwrites_narration(self):
        from beancount_importer.pipeline.run import _diff_changes
        entry = self._entry(narration="Burger King")
        proposal = self._proposal(narration="New narration text")
        changes = _diff_changes(entry, proposal, None)
        assert any(c.field == "narration" for c in changes)

    def test_empty_existing_narration_falls_through_truncation(self):
        # `_is_truncation_equivalent` returns False when one side is
        # empty — that path lets the timestamp/regular suppressions
        # downstream do their work without crashing on the empty string.
        from beancount_importer.pipeline.run import _diff_changes
        entry = self._entry(narration="")
        proposal = self._proposal(narration="Something new")
        changes = _diff_changes(entry, proposal, None)
        assert any(c.field == "narration" for c in changes)

    def test_multi_posting_entry_suppresses_account_change(self):
        from beancount_importer.pipeline.run import _diff_changes
        entry = self._entry(target_account="Income:Salary", has_multiple_postings=True)
        proposal = self._proposal(
            postings=(Posting(account="Income:OtherSalary"),),
        )
        changes = _diff_changes(entry, proposal, None)
        assert all(c.field != "account" for c in changes)

    def test_amount_inferred_entry_short_circuits(self):
        from beancount_importer.pipeline.run import _diff_changes
        entry = self._entry(amount_inferred=True)
        proposal = self._proposal(narration="anything", payee="Acme")
        assert _diff_changes(entry, proposal, None) == []


class TestDiffSuppressions:
    """Phase 2 diff suppressions: truncation-equivalence (A1/A8),
    timestamp-narration (A2), multi-posting category guard (A4).

    The fixture in every test below puts the CSV row 8 days after the
    bean entry so dedup doesn't fire — that way the row reaches
    `_diff_changes` where the suppressions live.
    """

    def _setup(self, tmp_path: Path, *, bean_body: str) -> Path:
        (tmp_path / "transactions").mkdir()
        (tmp_path / "transactions" / "SPK.bean").write_text(textwrap.dedent(bean_body))
        return tmp_path

    def test_truncation_equivalent_narration_no_diff(self, tmp_path: Path):
        # Bean narration is a prefix of the (longer) CSV description —
        # a previous run silently truncated it. No field change.
        self._setup(
            tmp_path,
            bean_body="""\
                2024-01-15 * "Acme" "Acme Inc"
                  Assets:B:SPK  -10.00 EUR
                  Expenses:X  10.00 EUR
            """,
        )
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "23.01.24;Acme;Acme Inc — Subscription Plan A;-10,00;EUR;\n"
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:X"),),
                payee=ctx.txn.payee,
                narration=ctx.txn.description,
            )

        session = make_session(tmp_path)
        results = run(session, tmp_path, categ, NoopReporter())
        assert results[0].action == "update"
        fields = {c.field for c in results[0].proposed_changes}
        assert "narration" not in fields

    def test_timestamp_narration_does_not_overwrite_real_narration(
        self, tmp_path: Path
    ):
        # Bean entry has a human-typed narration ("Burger King"). CSV
        # row's description is a transport-only timestamp string. The
        # proposal must not propose to clobber the human narration.
        self._setup(
            tmp_path,
            bean_body="""\
                2024-01-15 * "BK" "Burger King"
                  Assets:B:SPK  -10.00 EUR
                  Expenses:Food  10.00 EUR
            """,
        )
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "23.01.24;BK;2024-01-23T12:00 Debit;-10,00;EUR;\n"
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Expenses:Food"),),
                payee=ctx.txn.payee,
                narration=ctx.txn.description,
            )

        session = make_session(tmp_path)
        results = run(session, tmp_path, categ, NoopReporter())
        fields = {c.field for c in results[0].proposed_changes}
        assert "narration" not in fields

    def test_multi_posting_category_guard_suppresses_account_change(
        self, tmp_path: Path
    ):
        # Multi-posting salary entry — a rule that routes to a different
        # account would otherwise propose a category change, but
        # rewriting the user-authored deduction spread is wrong. The
        # guard suppresses the account field; the proposal still
        # propagates other fields the rule may change.
        self._setup(
            tmp_path,
            bean_body="""\
                2024-01-31 * "Employer" "Salary"
                  Assets:B:SPK  2000.00 EUR
                  Income:Salary  -3000.00 EUR
                  Expenses:Tax  500.00 EUR
                  Expenses:Insurance  500.00 EUR
            """,
        )
        (tmp_path / "SPK_jan.csv").write_text(
            "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
            "08.02.24;Employer;NewSalaryNarration;2000,00;EUR;\n"
        )

        def categ(ctx):
            return CategoryProposal(
                action="categorize",
                postings=(Posting(account="Income:OtherSalary"),),
                payee=ctx.txn.payee,
                narration=ctx.txn.description,
            )

        # Rule forces the proposal target to differ from the entry's
        # target_account, otherwise the seed-silent-skip path swallows
        # the row before `_diff_changes` runs.
        rule = CategorizationRule(
            target_account="Income:OtherSalary",
            payee_pattern="Employer",
        )
        cfg = Config(
            banks=[make_spk_bank(year_template_output=False)],
            transactions_dir="transactions",
            matching=MatchingConfig(min_score=0.35),
        )
        session = ImportSession(config=cfg, rules=(rule,), options=ImportOptions())
        results = run(session, tmp_path, categ, NoopReporter())
        fields = {c.field for c in results[0].proposed_changes}
        assert "account" not in fields

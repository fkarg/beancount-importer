from __future__ import annotations

from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest
from beancount.core import amount as bc_amount, data as bc_data

from beancount_importer.beancount_io.reader import (
    _extract_entry,
    read_ledger,
    read_ledger_multi,
    read_open_accounts,
)
from beancount_importer.beancount_io.writer import (
    append_entry,
    apply_update,
    format_transaction,
    splice_entries,
)
from beancount_importer.models import CategoryProposal, LedgerEntry, Posting

FIXTURES = Path(__file__).parent / "fixtures"


# ── reader ───────────────────────────────────────────────────────────────────

class TestReadLedger:
    def test_reads_three_transactions(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert len(entries) == 3

    def test_missing_file_returns_empty(self, tmp_path: Path):
        entries = read_ledger(tmp_path / "nonexistent.bean", "Assets:B:SPK")
        assert entries == []

    def test_first_entry_date(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].date == date(2024, 1, 15)

    def test_first_entry_narration(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].narration == "Netflix Abo"

    def test_first_entry_payee(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].payee == "Netflix"

    def test_first_entry_amount(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].amount == Decimal("-15.99")

    def test_first_entry_currency(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].currency == "EUR"

    def test_source_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].source_account == "Assets:B:SPK"

    def test_target_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].target_account == "Expenses:Entertainment"

    def test_sepa_ref_in_metadata(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].metadata.get("sepa_ref") == "NETFLIX-001"

    def test_untagged_entry_has_empty_tags(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].tags == ()

    def test_extracts_transaction_tags_sorted(self, tmp_path: Path):
        f = tmp_path / "tagged.bean"
        f.write_text(
            "2024-01-01 open Assets:B:SPK EUR\n"
            "2024-01-01 open Expenses:Travel EUR\n\n"
            '2024-03-25 * "Uber" "ride" #usa-2024 #trip\n'
            "  Assets:B:SPK    -25.00 EUR\n"
            "  Expenses:Travel  25.00 EUR\n"
        )
        entries = read_ledger(f, "Assets:B:SPK")
        assert entries[0].tags == ("trip", "usa-2024")

    def test_positive_amount_salary(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        salary = next(e for e in entries if "Gehalt" in e.narration)
        assert salary.amount == Decimal("3000.00")

    def test_no_entries_for_wrong_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:N26")
        assert entries == []


class TestReaderInternalMetadata:
    """beancount's loader injects reserved `__x__` keys (e.g. `__tolerances__`)
    onto transaction meta during booking. Those are not user metadata and are
    invalid as source syntax — they must not survive onto `LedgerEntry`, or a
    later `apply_update` would splice `__tolerances__: ...` and fail to reparse.
    """

    def test_dunder_txn_metadata_is_dropped(self):
        txn = bc_data.Transaction(
            meta={
                "filename": "x.bean",
                "lineno": 1,
                "__tolerances__": {"EUR": Decimal("0.005")},
                "sepa_ref": "KEEP-ME",
            },
            date=date(2024, 1, 15),
            flag="*",
            payee="P",
            narration="N",
            tags=frozenset(),
            links=frozenset(),
            postings=[
                bc_data.Posting(
                    "Assets:B:SPK",
                    bc_amount.Amount(Decimal("-1"), "EUR"),
                    None, None, None, None,
                ),
                bc_data.Posting(
                    "Expenses:X",
                    bc_amount.Amount(Decimal("1"), "EUR"),
                    None, None, None, None,
                ),
            ],
        )
        entry = _extract_entry(txn, "Assets:B:SPK", "x.bean", ())
        assert entry is not None
        assert "__tolerances__" not in entry.metadata
        assert entry.metadata["sepa_ref"] == "KEEP-ME"


class TestReadOpenAccounts:
    """`read_open_accounts` sources the authoritative chart from `open`
    directives — including accounts that carry no transaction yet, which the
    transaction-derived pool can never surface.
    """

    def test_returns_opened_accounts_including_unused(self, tmp_path: Path):
        f = tmp_path / "accounts.bean"
        f.write_text(
            "2024-01-01 open Assets:B:SPK EUR\n"
            "2024-01-01 open Expenses:Travel EUR\n"
            "2024-01-01 open Liabilities:Familie:Anna EUR\n\n"
            # A used account still shows; an unused open (Liabilities:Familie:Anna)
            # must show too — that's the whole point.
            '2024-03-25 * "Uber" "ride"\n'
            "  Assets:B:SPK    -25.00 EUR\n"
            "  Expenses:Travel  25.00 EUR\n"
        )
        assert read_open_accounts(f) == frozenset(
            {"Assets:B:SPK", "Expenses:Travel", "Liabilities:Familie:Anna"}
        )

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_open_accounts(tmp_path / "nope.bean") == frozenset()

    def test_resolves_includes(self, tmp_path: Path):
        (tmp_path / "leaf.bean").write_text(
            "2024-01-01 open Liabilities:CreditCard:Visa EUR\n"
        )
        main = tmp_path / "main.bean"
        main.write_text(
            'include "leaf.bean"\n'
            "2024-01-01 open Assets:B:SPK EUR\n"
        )
        assert read_open_accounts(main) == frozenset(
            {"Assets:B:SPK", "Liabilities:CreditCard:Visa"}
        )


class TestReadLedgerInferredAmount:
    """beancount fills in a missing posting amount during load. The reader
    needs to flag those entries (`amount_inferred=True`) so the scorer can
    treat them as cross-bank transit legs.
    """

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_explicit_amount_not_marked_inferred(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-01-15 * "Netflix" "Abo"\n'
            '  Assets:B:SPK  -15.99 EUR\n'
            '  Expenses:Entertainment  15.99 EUR\n',
        )
        entries = read_ledger(bean, "Assets:B:SPK")
        assert entries[0].amount_inferred is False

    def test_inferred_amount_marked(self, tmp_path: Path):
        # SPK→PayPal transfer: SPK has explicit amount, PayPal leg is inferred.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-18 * "PayPal" "Einkauf"\n'
            '  Assets:B:SPK  -10.00 EUR\n'
            '  Assets:B:PayPal\n',
        )
        entries = read_ledger(bean, "Assets:B:PayPal")
        assert len(entries) == 1
        assert entries[0].amount == Decimal("10.00")
        assert entries[0].amount_inferred is True


class TestReadLedgerMultiPosting:
    """Salary entries (and similar) carry deduction legs that mean the parent
    transaction has >2 postings. The reader surfaces this via
    `has_multiple_postings` so the diff layer can refuse category clobbering.
    """

    def test_two_posting_entry_not_marked(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        bean.write_text(
            '2024-01-15 * "Netflix" "Abo"\n'
            '  Assets:B:SPK  -15.99 EUR\n'
            '  Expenses:Entertainment  15.99 EUR\n',
        )
        entries = read_ledger(bean, "Assets:B:SPK")
        assert entries[0].has_multiple_postings is False

    def test_multi_posting_salary_marked(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        bean.write_text(
            '2024-01-31 * "Employer" "Salary"\n'
            '  Assets:B:SPK  2000.00 EUR\n'
            '  Income:Salary  -3000.00 EUR\n'
            '  Expenses:Tax  500.00 EUR\n'
            '  Expenses:Insurance  500.00 EUR\n',
        )
        entries = read_ledger(bean, "Assets:B:SPK")
        assert entries[0].has_multiple_postings is True


class TestReadLedgerMetadataDates:
    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_extracts_actual_date(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-01-19 * "Merchant" "PayPal"\n'
            '  Assets:B:SPK  -103.19 EUR\n'
            '  Assets:B:PayPal\n'
            '    actual: 2024-01-17\n',
        )
        entries = read_ledger(bean, "Assets:B:PayPal")
        assert len(entries) == 1
        assert date(2024, 1, 17) in entries[0].metadata_dates

    def test_unconfigured_keys_ignored(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-01-19 * "Merchant" "PayPal"\n'
            '  Assets:B:SPK  -100 EUR\n'
            '  Assets:B:PayPal\n'
            '    actual: 2024-01-17\n',
        )
        # Only consider `paypal:` — `actual:` should be ignored.
        entries = read_ledger(
            bean, "Assets:B:PayPal", metadata_date_keys=("paypal",)
        )
        assert entries[0].metadata_dates == ()


class TestReadLedgerSynthesize:
    """When a user runs plugins that split transactions at load time, the
    bean file may carry only a metadata hint (`paypal: 2024-01-17`). The
    reader can synthesise the virtual entry the plugin would produce so
    that cross-bank PayPal matching works without loading plugins.
    """

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_synthesizes_paypal_entry_from_metadata(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        # Bank-side entry where the user's plugin would split off a PayPal
        # transaction on the actual purchase date.
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        # No real PayPal posting → only the synthesized one.
        assert len(entries) == 1
        e = entries[0]
        assert e.date == date(2024, 4, 11)
        assert e.amount == Decimal("-3.39")
        assert e.target_account == "Expenses:Apps"
        assert e.amount_inferred is True

    def test_synthesize_ignored_when_account_doesnt_match(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        # Loading for SPK shouldn't trigger PayPal synthesis.
        entries = read_ledger(
            bean,
            "Assets:B:SPK",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].source_account == "Assets:B:SPK"
        assert entries[0].amount_inferred is False

    def test_unparseable_metadata_date_skips_synthesis(self, tmp_path: Path):
        # A user-typed `paypal: "garbage"` shouldn't crash or synthesise — it
        # should be silently ignored. _coerce_date returns None and the
        # synthesise loop continues without emitting.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: "garbage-date"\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        # No virtual entry was synthesised — the unparseable date was ignored.
        assert entries == []

    def test_quoted_string_metadata_date_parsed(self, tmp_path: Path):
        # When the user types `paypal: "2024-04-11"` (note the quotes) the
        # value comes through as `str` rather than `datetime.date`. The
        # reader's `_coerce_date` must handle that path too.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: "2024-04-11"\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].date == date(2024, 4, 11)

    def test_quoted_dotted_date_format_parsed(self, tmp_path: Path):
        # Some legacy ledgers wrote dates as `paypal: "11.04.2024"`. The
        # reader cycles through ISO, dotted, and slash formats — covers
        # the second item in the format tuple.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: "11.04.2024"\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].date == date(2024, 4, 11)

    def test_synthesize_picks_first_expense_when_multiple(self, tmp_path: Path):
        # Three postings: source bank + two Expenses legs. `_pick_other_posting`
        # must keep the FIRST Expenses match and skip subsequent ones.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Y" ""\n'
            '  Assets:B:SPK  -10.00 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:First  6.00 EUR\n'
            '  Expenses:Second  4.00 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].target_account == "Expenses:First"

    def test_pick_other_posting_returns_none_when_only_source(self, tmp_path: Path):
        # Edge case: a transaction with only one posting (the source). Beancount
        # does accept such structures via padding/automatic — exercise the
        # `_pick_other_posting` returning None path.
        from beancount_importer.beancount_io.reader import _pick_other_posting
        from collections import namedtuple

        FakePosting = namedtuple("FakePosting", ["account"])
        src = FakePosting(account="Assets:B:SPK")
        # Single posting list containing only the source: no "other" exists.
        assert _pick_other_posting([src], src) is None

    def test_pick_other_posting_falls_back_to_non_expense(self):
        # Cover the `elif fallback is None` branch in _pick_other_posting:
        # when no Expenses/Income posting exists, the first non-source
        # posting is used.
        from beancount_importer.beancount_io.reader import _pick_other_posting
        from collections import namedtuple

        FakePosting = namedtuple("FakePosting", ["account"])
        src = FakePosting(account="Assets:B:SPK")
        other = FakePosting(account="Assets:B:N26")
        third = FakePosting(account="Assets:B:Other")
        # Multiple non-expense postings — fallback picks the first non-source
        # one and ignores the rest.
        assert _pick_other_posting([src, other, third], src) is other


class TestCoerceDate:
    """Unit tests for `_coerce_date` covering each input shape it accepts."""

    def test_date_passthrough(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        d = date(2024, 1, 15)
        assert _coerce_date(d) == d

    def test_datetime_truncated_to_date(self):
        from datetime import datetime
        from beancount_importer.beancount_io.reader import _coerce_date
        # Beancount metadata can carry either `date` or `datetime`; the
        # latter must be reduced to a date so callers compare apples-to-apples.
        assert _coerce_date(datetime(2024, 1, 15, 10, 30)) == date(2024, 1, 15)

    def test_iso_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("2024-01-15") == date(2024, 1, 15)

    def test_quoted_iso_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        # Beancount preserves quotes around metadata strings. Strip them
        # before parsing.
        assert _coerce_date('"2024-01-15"') == date(2024, 1, 15)

    def test_dotted_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("15.01.2024") == date(2024, 1, 15)

    def test_slashed_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("15/01/2024") == date(2024, 1, 15)

    def test_garbage_string_returns_none(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("not-a-date") is None

    def test_non_string_non_date_returns_none(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        # Integers and other non-date-like values should fall through to None
        # rather than raise.
        assert _coerce_date(12345) is None
        assert _coerce_date(None) is None


class TestReadLedgerMulti:
    """Multi-account variant: emits one entry per matching posting on a Transaction.

    Matching is by explicit account membership OR by `account_prefixes`. Both
    can be supplied together. With neither supplied, the call returns an empty
    list (callers should always say what they want).
    """

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_no_filters_returns_empty(self, tmp_path: Path):
        bean = tmp_path / "x.bean"
        self._write(
            bean,
            '2024-01-15 * "X" ""\n'
            '  Assets:B:SPK  -1 EUR\n'
            '  Expenses:Other  1 EUR\n',
        )
        assert read_ledger_multi(bean) == []

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_ledger_multi(
            tmp_path / "nope.bean", account_prefixes=("Assets:B:",)
        ) == []

    def test_emits_one_entry_per_matching_posting(self, tmp_path: Path):
        # SPK→N26 transfer has two bank-shaped legs; multi-mode exposes both.
        bean = tmp_path / "x.bean"
        self._write(
            bean,
            '2024-02-01 * "transfer" ""\n'
            '  Assets:B:SPK  -100 EUR\n'
            '  Assets:B:N26  100 EUR\n',
        )
        entries = read_ledger_multi(bean, account_prefixes=("Assets:B:",))
        accounts = sorted(e.source_account for e in entries)
        assert accounts == ["Assets:B:N26", "Assets:B:SPK"]

    def test_explicit_accounts_take_precedence(self, tmp_path: Path):
        bean = tmp_path / "x.bean"
        self._write(
            bean,
            '2024-02-01 * "transfer" ""\n'
            '  Assets:B:SPK  -100 EUR\n'
            '  Assets:B:N26  100 EUR\n',
        )
        # No prefix; only SPK is explicitly named.
        entries = read_ledger_multi(bean, accounts=("Assets:B:SPK",))
        assert [e.source_account for e in entries] == ["Assets:B:SPK"]

    def test_synthesis_only_for_in_scope_targets(self, tmp_path: Path):
        bean = tmp_path / "x.bean"
        self._write(
            bean,
            '2024-04-13 * "Google" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        # PayPal not in scope → no synthesised entry.
        entries = read_ledger_multi(
            bean,
            accounts=("Assets:B:SPK",),
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert [e.source_account for e in entries] == ["Assets:B:SPK"]

        # PayPal added to scope via prefix → SPK entry plus synthesised PayPal entry.
        entries2 = read_ledger_multi(
            bean,
            account_prefixes=("Assets:B:",),
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        accounts = sorted(e.source_account for e in entries2)
        assert accounts == ["Assets:B:PayPal", "Assets:B:SPK"]

    def test_synthesis_filter_skips_unmapped_keys(self, tmp_path: Path):
        # Two synth keys mapping to different accounts; only one is in scope.
        # Exercises `_synthesize_virtual_entries`'s `mapped != source_account`
        # branch when the synth_map carries entries irrelevant to the current
        # synthesis target.
        bean = tmp_path / "x.bean"
        self._write(
            bean,
            '2024-04-13 * "Google" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger_multi(
            bean,
            accounts=("Assets:B:PayPal",),
            synthesize_from_metadata={
                "paypal": "Assets:B:PayPal",
                "settle": "Assets:B:Other",  # not in scope, must be filtered
            },
        )
        assert len(entries) == 1
        assert entries[0].source_account == "Assets:B:PayPal"
        assert entries[0].date == date(2024, 4, 11)


# ── writer: format_transaction ───────────────────────────────────────────────

class TestFormatTransaction:
    def test_basic_format(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee="Netflix",
            narration="Netflix Abo",
            postings=[
                ("Assets:B:SPK", "-15.99 EUR"),
                ("Expenses:Entertainment", "15.99 EUR"),
            ],
        )
        assert "2024-01-15 * " in text
        assert '"Netflix"' in text
        assert '"Netflix Abo"' in text
        assert "Assets:B:SPK" in text
        assert "-15.99 EUR" in text

    def test_no_payee(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Transfer",
            postings=[("Assets:B:SPK", "-100 EUR"), ("Assets:B:N26", "100 EUR")],
        )
        assert '"Transfer"' in text
        # No extra quotes for missing payee
        assert text.count('"Transfer"') == 1

    def test_metadata_included(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
            metadata={"sepa_ref": "REF-001"},
        )
        assert 'sepa_ref: "REF-001"' in text

    def test_ends_with_newline(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
        )
        assert text.endswith("\n")

    def test_posting_level_metadata_rendered(self):
        text = format_transaction(
            date_str="2024-03-15",
            flag="*",
            payee="Acme",
            narration="x-bank",
            postings=[
                ("Assets:B:SPK", "-100 EUR", {}),
                ("Assets:B:N26", "100 EUR", {"settle": "2024-03-17"}),
            ],
        )
        # Posting line plus a metadata line indented two more spaces.
        assert "  Assets:B:N26" in text
        # Date-shaped metadata is written bare (beancount date, not str).
        assert "    settle: 2024-03-17" in text
        # The metadata line follows its posting line.
        lines = text.splitlines()
        n26_idx = next(i for i, line in enumerate(lines) if "Assets:B:N26" in line)
        assert lines[n26_idx + 1].strip().startswith("settle:")

    def test_narration_truncated_when_max_length_set(self):
        long = "x" * 200
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration=long,
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
            narration_max_length=70,
        )
        assert '"' + "x" * 70 + '"' in text
        assert "x" * 71 not in text

    def test_narration_not_truncated_below_threshold(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="short",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
            narration_max_length=70,
        )
        assert '"short"' in text

    def test_tags_rendered_as_hashtags_on_header(self):
        # beancount tags are #hashtags on the transaction header line, never
        # `tag:` metadata.
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee="P",
            narration="N",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:X", "1 EUR")],
            tags=("usa-2024", "trip"),
        )
        assert text.splitlines()[0] == '2024-01-15 * "P" "N" #usa-2024 #trip'
        assert "tag:" not in text

    def test_tags_normalize_stray_leading_hash(self):
        # Internally tags are bare, but a user who typed "#trip" must not yield
        # a double-hash "##trip".
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="N",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:X", "1 EUR")],
            tags=("#trip",),
        )
        assert " #trip" in text.splitlines()[0]
        assert "##trip" not in text

    def test_iso_date_metadata_written_bare_not_quoted(self):
        # beancount parses `settle: "2024-01-17"` as a str but `settle:
        # 2024-01-17` as a date. Plugins (settle/actual) compare against a
        # date, so date-shaped metadata must be emitted bare.
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[
                ("Assets:B:SPK", "-1 EUR", {"settle": "2024-01-17"}),
                ("Expenses:Other", "1 EUR", {}),
            ],
            metadata={"actual": "2024-05-13", "sepa_ref": "REF-123"},
        )
        assert "actual: 2024-05-13" in text
        assert 'actual: "2024-05-13"' not in text
        assert "settle: 2024-01-17" in text
        # Non-date metadata stays quoted.
        assert 'sepa_ref: "REF-123"' in text

    def test_internal_dunder_metadata_is_dropped(self):
        # Reserved `__x__` keys are not valid beancount source syntax; the
        # writer must never emit them, at either the txn or posting level.
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[
                ("Assets:B:SPK", "-1 EUR", {"__automatic__": "True"}),
                ("Expenses:Other", "1 EUR", {}),
            ],
            metadata={"__tolerances__": "{'EUR': 0.005}", "sepa_ref": "REF-001"},
        )
        assert "__tolerances__" not in text
        assert "__automatic__" not in text
        assert 'sepa_ref: "REF-001"' in text


class TestApplyUpdateRobustness:
    """Regression: an entry loaded via beancount carries `__tolerances__` on
    its metadata; splicing it back must produce reparse-valid beancount rather
    than crashing with `Invalid token: '__tolerances__:'` and rolling back.
    """

    def test_apply_update_survives_internal_metadata(self, tmp_path: Path):
        f = tmp_path / "SPK.bean"
        f.write_text(
            '2024-01-15 * "Netflix" "Old narration"\n'
            "  Assets:B:SPK            -15.99 EUR\n"
            "  Expenses:Old             15.99 EUR\n"
        )
        entry = LedgerEntry(
            date=date(2024, 1, 15),
            flag="*",
            payee="Netflix",
            narration="Old narration",
            source_account="Assets:B:SPK",
            target_account="Expenses:Old",
            amount=Decimal("-15.99"),
            currency="EUR",
            metadata={
                "__tolerances__": "{'EUR': Decimal('0.005')}",
                "sepa_ref": "NFX",
            },
            line_start=1,
            file_path=str(f),
        )
        proposal = CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:New"),),
            narration="New narration",
        )
        # Would raise "beancount parse failed after splice — rolled back" if the
        # reserved key were written.
        apply_update(entry, proposal, "Assets:B:SPK")
        out = f.read_text()
        assert "__tolerances__" not in out
        assert 'sepa_ref: "NFX"' in out
        assert "Expenses:New" in out

    def test_apply_update_writes_tag_as_hashtag_merging_existing(
        self, tmp_path: Path
    ):
        f = tmp_path / "SPK.bean"
        f.write_text(
            '2024-01-15 * "Netflix" "Old" #existing\n'
            "  Assets:B:SPK   -15.99 EUR\n"
            "  Expenses:Old    15.99 EUR\n"
        )
        entry = LedgerEntry(
            date=date(2024, 1, 15),
            flag="*",
            payee="Netflix",
            narration="Old",
            source_account="Assets:B:SPK",
            target_account="Expenses:Old",
            amount=Decimal("-15.99"),
            currency="EUR",
            tags=("existing",),
            line_start=1,
            file_path=str(f),
        )
        proposal = CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:New"),),
            tag="fresh",
        )
        apply_update(entry, proposal, "Assets:B:SPK")
        header = f.read_text().splitlines()[0]
        # Existing header tag preserved, new proposal tag added — both as #tags.
        assert "#existing" in header
        assert "#fresh" in header
        assert "tag:" not in f.read_text()

    def test_apply_update_migrates_legacy_tag_metadata(self, tmp_path: Path):
        # Entries written by the old (buggy) code carry `tag: "..."` metadata.
        # Rewriting one should promote it to a real #tag, not re-emit metadata.
        f = tmp_path / "SPK.bean"
        f.write_text(
            '2024-01-15 * "Netflix" "Old"\n'
            '  tag: "oldstyle"\n'
            "  Assets:B:SPK   -15.99 EUR\n"
            "  Expenses:Old    15.99 EUR\n"
        )
        entry = LedgerEntry(
            date=date(2024, 1, 15),
            flag="*",
            payee="Netflix",
            narration="Old",
            source_account="Assets:B:SPK",
            target_account="Expenses:Old",
            amount=Decimal("-15.99"),
            currency="EUR",
            metadata={"tag": "oldstyle"},
            line_start=1,
            file_path=str(f),
        )
        proposal = CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:New"),),
        )
        apply_update(entry, proposal, "Assets:B:SPK")
        out = f.read_text()
        assert "#oldstyle" in out.splitlines()[0]
        assert 'tag: "oldstyle"' not in out


# ── writer: append_entry ─────────────────────────────────────────────────────

class TestAppendEntry:
    def test_appends_to_new_file(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        append_entry("2024-01-15 * \"Netflix\" \"test\"\n  Assets:B:SPK  -15 EUR\n", target)
        assert target.exists()
        assert "Netflix" in target.read_text()

    def test_dry_run_does_not_write(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        append_entry("...", target, dry_run=True)
        assert not target.exists()

    def test_appends_to_existing(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        target.write_text("first entry\n")
        append_entry("second entry\n", target)
        content = target.read_text()
        assert "first entry" in content
        assert "second entry" in content

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "sub" / "dir" / "SPK.bean"
        append_entry("entry\n", target)
        assert target.exists()


class TestSpliceEntries:
    """Cover the back-to-front splicing invariant called out in CLAUDE.md.

    Splicing front-to-back would shift the line numbers of every subsequent
    splice, corrupting the file. The writer sorts splices by `line_start`
    descending so that earlier-line splices stay accurate even after later
    ones replace differently-sized blocks. Subprocess calls to `bean-check`
    are mocked here — we don't need the binary to validate the logic.
    """

    def _stub_syntax_ok(self, monkeypatch):
        """Make the post-splice validation always pass, so line-manipulation
        tests can use non-beancount placeholder content (`L1`, `L2`, …)."""
        from beancount_importer.beancount_io import writer as writer_mod

        monkeypatch.setattr(writer_mod, "_syntax_errors", lambda content: [])

    def test_splices_apply_back_to_front(self, tmp_path: Path, monkeypatch):
        # Replace two non-overlapping single-line ranges where the EARLIER
        # replacement is multi-line and the LATER one is single-line. If
        # splicing went front-to-back, the L2→[A1,A2] replacement would shift
        # L5 down by one line, and the (5,5) splice would corrupt L6 instead.
        # Sorting back-to-front sidesteps this.
        target = tmp_path / "x.bean"
        target.write_text(
            "L1\n"
            "L2\n"
            "L3\n"
            "L4\n"
            "L5\n"
            "L6\n"
        )
        self._stub_syntax_ok(monkeypatch)
        splice_entries(
            [
                # (start, end) ranges are 1-based inclusive — `start` and
                # `end` equal selects exactly one line.
                (2, 2, "A1\nA2"),  # L2 → two lines
                (5, 5, "B1"),       # L5 → one line
            ],
            target,
        )
        content = target.read_text().splitlines()
        # L2 expanded to two lines; L5 replaced by one line; everything else intact.
        assert content == ["L1", "A1", "A2", "L3", "L4", "B1", "L6"]

    def test_dry_run_does_not_modify(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x.bean"
        target.write_text("hello\n")
        # Even with validation stubbed, dry_run should short-circuit before
        # touching the file (and never validate).
        called = []
        from beancount_importer.beancount_io import writer as writer_mod
        monkeypatch.setattr(
            writer_mod, "_syntax_errors", lambda content: called.append(content) or []
        )
        splice_entries([(1, 1, "OOPS")], target, dry_run=True)
        assert target.read_text() == "hello\n"
        assert called == []  # validation not invoked

    def test_empty_updates_is_noop(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x.bean"
        target.write_text("hello\n")
        called = []
        from beancount_importer.beancount_io import writer as writer_mod
        monkeypatch.setattr(
            writer_mod, "_syntax_errors", lambda content: called.append(content) or []
        )
        splice_entries([], target)
        assert target.read_text() == "hello\n"
        # No validation needed when there's nothing to splice.
        assert called == []

    def test_syntax_error_rolls_back(self, tmp_path: Path):
        # Critical: if the splice produces structurally broken beancount, the
        # writer restores the .bak and raises so the ledger is never corrupted.
        # Uses the real parser (no stub) — splicing garbage into a valid file.
        target = tmp_path / "x.bean"
        original = (
            '2024-01-02 * "shop" "x"\n'
            "  Assets:B:SPK   -5.00 EUR\n"
            "  Expenses:Foo    5.00 EUR\n"
        )
        target.write_text(original)
        with pytest.raises(RuntimeError, match="parse failed"):
            splice_entries([(2, 2, "  this is not valid beancount @@ @@ ((")], target)
        assert target.read_text() == original
        assert not target.with_suffix(target.suffix + ".bak").exists()

    def test_unknown_accounts_do_not_roll_back(self, tmp_path: Path):
        # Regression: the per-year files are fragments with no `open`
        # directives, so a full bean-check would flag every account as
        # "unknown" and roll back. Parse-only validation must let the write
        # through — the accounts exist in the root ledger.
        target = tmp_path / "SPK.bean"
        target.write_text(
            '2024-01-02 * "old" "x"\n'
            "  Assets:B:SPK   -5.00 EUR\n"
            "  Expenses:Old    5.00 EUR\n"
        )
        new = (
            '2024-01-02 * "Hetzner Online GmbH" "Invoice"\n'
            "  Assets:B:SPK              -5.11 EUR\n"
            "  Expenses:PersonalCompute   5.11 EUR\n"
        )
        splice_entries([(1, 3, new)], target)
        assert "Expenses:PersonalCompute" in target.read_text()
        assert not target.with_suffix(target.suffix + ".bak").exists()

    def test_successful_splice_removes_backup(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x.bean"
        target.write_text("L1\nL2\nL3\n")
        self._stub_syntax_ok(monkeypatch)
        splice_entries([(2, 2, "REPLACED")], target)
        # Backup is cleaned up on success.
        assert not target.with_suffix(target.suffix + ".bak").exists()


# ── writer: splice-target guard ──────────────────────────────────────────────

class TestApplyUpdateTargetGuard:
    """`apply_update` must refuse to splice when `line_start` no longer points
    at the entry's own header line. Stale coordinates (e.g. after an earlier
    splice shifted the file) would otherwise silently rewrite whatever
    innocent transaction sits there — the 2024 ledger-corruption incident.
    """

    PROPOSAL = CategoryProposal(
        action="categorize", postings=(Posting(account="Expenses:New"),)
    )

    def _ledger(self, tmp_path: Path) -> Path:
        f = tmp_path / "SPK.bean"
        f.write_text(
            '2024-01-15 * "Netflix" "Old"\n'
            "  Assets:B:SPK   -15.99 EUR\n"
            "  Expenses:Old    15.99 EUR\n"
            "\n"
            '2024-01-20 * "Rewe" "Food"\n'
            "  Assets:B:SPK   -42.50 EUR\n"
            "  Expenses:Food   42.50 EUR\n"
        )
        return f

    def _entry(self, f: Path, line_start: int) -> LedgerEntry:
        return LedgerEntry(
            date=date(2024, 1, 15),
            payee="Netflix",
            narration="Old",
            source_account="Assets:B:SPK",
            target_account="Expenses:Old",
            amount=Decimal("-15.99"),
            line_start=line_start,
            file_path=str(f),
        )

    def test_line_start_on_wrong_transaction_raises_untouched(self, tmp_path: Path):
        # line 5 is the Rewe header — a date line, but the wrong date. This is
        # exactly the shape stale coordinates produce after an earlier splice.
        f = self._ledger(tmp_path)
        original = f.read_text()
        with pytest.raises(ValueError, match="mismatch"):
            apply_update(self._entry(f, 5), self.PROPOSAL, "Assets:B:SPK")
        assert f.read_text() == original

    def test_line_start_on_posting_line_raises_untouched(self, tmp_path: Path):
        f = self._ledger(tmp_path)
        original = f.read_text()
        with pytest.raises(ValueError, match="mismatch"):
            apply_update(self._entry(f, 2), self.PROPOSAL, "Assets:B:SPK")
        assert f.read_text() == original

    def test_line_start_beyond_eof_raises_untouched(self, tmp_path: Path):
        f = self._ledger(tmp_path)
        original = f.read_text()
        with pytest.raises(ValueError, match="mismatch"):
            apply_update(self._entry(f, 99), self.PROPOSAL, "Assets:B:SPK")
        assert f.read_text() == original

    def test_correct_line_start_still_splices(self, tmp_path: Path):
        f = self._ledger(tmp_path)
        apply_update(self._entry(f, 1), self.PROPOSAL, "Assets:B:SPK")
        out = f.read_text()
        assert "Expenses:New" in out
        assert 'Rewe' in out  # neighbour untouched


# ── links: reader → model → writer round-trip ────────────────────────────────

class TestLinksRoundTrip:
    """Transaction `^links` (e.g. the `^xfer-...` pair binding both legs of a
    cross-bank transfer) must survive an update rewrite. Previously
    `format_transaction` had no links parameter, so every splice silently
    dropped them.
    """

    def test_reader_extracts_links_sorted(self, tmp_path: Path):
        f = tmp_path / "l.bean"
        f.write_text(
            '2024-05-15 * "Steam" "game" ^xfer-b ^xfer-a\n'
            "  Assets:B:SPK    -33.82 EUR\n"
            "  Assets:B:PayPal  33.82 EUR\n"
        )
        entries = read_ledger(f, "Assets:B:SPK")
        assert entries[0].links == ("xfer-a", "xfer-b")

    def test_format_transaction_renders_links_after_tags(self):
        text = format_transaction(
            date_str="2024-05-15",
            flag="*",
            payee="Steam",
            narration="game",
            postings=[("Assets:B:SPK", "-33.82 EUR"), ("Assets:B:PayPal", None)],
            tags=("t",),
            links=("xfer-a",),
        )
        assert text.splitlines()[0] == '2024-05-15 * "Steam" "game" #t ^xfer-a'

    def test_format_transaction_normalizes_stray_leading_caret(self):
        text = format_transaction(
            date_str="2024-05-15",
            flag="*",
            payee=None,
            narration="game",
            postings=[("Assets:B:SPK", "-33.82 EUR")],
            links=("^xfer-a", ""),
        )
        header = text.splitlines()[0]
        assert header.endswith(" ^xfer-a")
        assert "^^" not in header

    def test_apply_update_preserves_links(self, tmp_path: Path):
        f = tmp_path / "SPK.bean"
        f.write_text(
            '2024-05-15 * "Steam" "game" ^xfer-1\n'
            "  Assets:B:SPK    -33.82 EUR\n"
            "  Expenses:Old     33.82 EUR\n"
        )
        entry = read_ledger(f, "Assets:B:SPK")[0]
        proposal = CategoryProposal(
            action="categorize", postings=(Posting(account="Expenses:Games"),)
        )
        apply_update(entry, proposal, "Assets:B:SPK")
        out = f.read_text()
        assert "^xfer-1" in out.splitlines()[0]
        assert "Expenses:Games" in out

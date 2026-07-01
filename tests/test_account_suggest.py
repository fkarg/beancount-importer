"""Account suggestion ranking."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given, strategies as st

from beancount_importer.matching.account_suggest import (
    account_glyph,
    is_self_transfer,
    rank_accounts,
)
from beancount_importer.models import LedgerEntry, SourceTransaction


def _txn(amount: Decimal = Decimal("-10.00")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 1, 1),
        amount=amount,
        currency="EUR",
        bank_key="spk",
    )


def _entry(source: str, target: str, amount: Decimal = Decimal("-10.00")) -> LedgerEntry:
    return LedgerEntry(
        date=date(2024, 1, 1),
        narration="x",
        source_account=source,
        target_account=target,
        amount=amount,
        currency="EUR",
    )


class TestIsSelfTransfer:
    def test_matches_case_and_accent_insensitively(self):
        assert is_self_transfer("FELIX KARG", ["Félix Karg"])

    def test_matches_as_substring_of_longer_payee(self):
        assert is_self_transfer("Felix Karg (savings)", ["Felix Karg"])

    def test_non_matching_payee_is_false(self):
        assert not is_self_transfer("Netflix", ["Felix Karg"])

    def test_empty_payee_is_false(self):
        assert not is_self_transfer(None, ["Felix Karg"])
        assert not is_self_transfer("", ["Felix Karg"])

    def test_empty_owner_names_is_false(self):
        assert not is_self_transfer("Felix Karg", [])

    def test_blank_owner_name_never_matches(self):
        # A stray "" in owner_names must not turn every payee into a transfer.
        assert not is_self_transfer("Netflix", ["  "])


class TestRankAccounts:
    def test_boost_prefixes_sort_own_accounts_first(self):
        existing = [
            _entry("Assets:B:SPK", "Expenses:Food"),
            _entry("Assets:B:SPK", "Expenses:Food"),  # frequency-heavy expense
            _entry("Assets:B:N26", "Liabilities:CreditCard:NO"),
        ]
        top, _ = rank_accounts(
            _txn(), [], existing, boost_prefixes=("Assets:B:", "Liabilities:CreditCard:")
        )
        # Own accounts precede the far more frequent Expenses:Food.
        assert top[0].startswith(("Assets:B:", "Liabilities:CreditCard:"))
        assert top.index("Liabilities:CreditCard:NO") < top.index("Expenses:Food")

    def test_candidate_target_outranks_others(self):
        existing = [
            _entry("Assets:B:SPK", "Expenses:Food"),
            _entry("Assets:B:SPK", "Expenses:Rent"),
        ]
        # The candidate's target should bubble to the top.
        candidate_entry = _entry("Assets:B:SPK", "Expenses:Rent")
        top, _ = rank_accounts(_txn(), [(candidate_entry, 0.9)], existing)
        assert top[0] == "Expenses:Rent"

    def test_suggested_target_pinned_at_index_zero(self):
        existing = [
            _entry("Assets:B:SPK", "Expenses:Food"),
            _entry("Assets:B:SPK", "Expenses:Rent"),
        ]
        top, _ = rank_accounts(
            _txn(), [], existing, suggested_target="Expenses:Custom"
        )
        assert top[0] == "Expenses:Custom"
        # Suggested target is pinned even when not in the existing pool.
        assert "Expenses:Custom" not in {
            e.target_account for e in existing
        }

    def test_debit_prefers_expenses_over_income(self):
        existing = [
            _entry("Assets:B:SPK", "Income:Salary", Decimal("3000")),
            _entry("Assets:B:SPK", "Expenses:Food"),
        ]
        top, _ = rank_accounts(_txn(Decimal("-10.00")), [], existing)
        assert top.index("Expenses:Food") < top.index("Income:Salary")

    def test_credit_prefers_income_over_expenses(self):
        existing = [
            _entry("Assets:B:SPK", "Income:Salary", Decimal("3000")),
            _entry("Assets:B:SPK", "Expenses:Food"),
        ]
        top, _ = rank_accounts(_txn(Decimal("3000")), [], existing)
        assert top.index("Income:Salary") < top.index("Expenses:Food")

    def test_top_n_is_respected(self):
        existing = [_entry("Assets:B:SPK", f"Expenses:Cat{i}") for i in range(20)]
        top, all_ = rank_accounts(_txn(), [], existing, top_n=5)
        assert len(top) == 5
        # `all` returns every known account regardless of top_n.
        assert len(all_) == 21  # 20 expenses + Assets:B:SPK

    def test_empty_inputs_yield_empty_lists(self):
        top, all_ = rank_accounts(_txn(), [], [])
        assert top == []
        assert all_ == []

    def test_known_accounts_appear_in_all_but_not_suggestions(self):
        # `known_accounts` is the authoritative chart (opened accounts).
        # Opened-but-unused accounts must show up in the full `all` pool the
        # `[l]` picker lists, but must NOT pollute the ranked suggestions —
        # they carry no usage signal.
        existing = [_entry("Assets:B:SPK", "Expenses:Food")]
        top, all_ = rank_accounts(
            _txn(),
            [],
            existing,
            known_accounts=("Liabilities:Familie:Anna", "Expenses:Food"),
        )
        assert "Liabilities:Familie:Anna" in all_
        assert "Liabilities:Familie:Anna" not in top
        # Used accounts still rank as suggestions.
        assert "Expenses:Food" in top

    def test_known_accounts_deduped_against_usage(self):
        existing = [_entry("Assets:B:SPK", "Expenses:Food")]
        _, all_ = rank_accounts(
            _txn(), [], existing, known_accounts=("Expenses:Food", "Assets:B:SPK")
        )
        assert sorted(all_) == ["Assets:B:SPK", "Expenses:Food"]

    @given(
        amount=st.decimals(allow_nan=False, allow_infinity=False, places=2).filter(
            lambda d: d != 0
        ),
        n=st.integers(min_value=1, max_value=20),
    )
    def test_top_n_property(self, amount: Decimal, n: int):
        existing = [_entry("Assets:B:SPK", f"Expenses:Cat{i}") for i in range(15)]
        top, _ = rank_accounts(
            _txn(amount), [], existing, top_n=n, suggested_target="Expenses:Pinned"
        )
        assert len(top) <= n + 1  # pinned suggestion may push us over by 1
        assert top[0] == "Expenses:Pinned"

    def test_entries_with_blank_account_strings_are_skipped(self):
        # Synthesized virtual entries can carry empty target_account when no
        # other posting was found. They must not contribute "" to the rankings.
        existing = [
            _entry("", "Expenses:Food"),
            _entry("Assets:B:SPK", ""),
        ]
        top, all_ = rank_accounts(_txn(), [], existing)
        assert "" not in all_
        assert "" not in top

    def test_equity_accounts_get_neutral_sign_score(self):
        # An account that doesn't fit any sign-bias prefix (e.g., Equity)
        # should still appear in the rankings, just at the neutral tier.
        existing = [
            _entry("Assets:B:SPK", "Equity:Opening-Balances"),
            _entry("Assets:B:SPK", "Expenses:Food"),
        ]
        top, _ = rank_accounts(_txn(Decimal("-10.00")), [], existing)
        assert "Equity:Opening-Balances" in top
        # Expenses still beats Equity for a debit.
        assert top.index("Expenses:Food") < top.index("Equity:Opening-Balances")

    def test_equity_neutral_score_under_credit_too(self):
        # Same neutral-tier behaviour when the txn is a credit. Income
        # outranks Equity, which still appears in the list.
        existing = [
            _entry("Assets:B:SPK", "Equity:Opening-Balances", Decimal("100")),
            _entry("Assets:B:SPK", "Income:Salary", Decimal("3000")),
        ]
        top, _ = rank_accounts(_txn(Decimal("3000")), [], existing)
        assert top.index("Income:Salary") < top.index("Equity:Opening-Balances")

    def test_suggested_target_in_existing_pool_not_duplicated(self):
        # If the rule's target is already a known account, it must appear
        # exactly once at index 0, not twice.
        existing = [
            _entry("Assets:B:SPK", "Expenses:Food"),
            _entry("Assets:B:SPK", "Expenses:Rent"),
        ]
        top, _ = rank_accounts(
            _txn(), [], existing, suggested_target="Expenses:Food"
        )
        assert top[0] == "Expenses:Food"
        assert top.count("Expenses:Food") == 1


class TestAccountGlyph:
    def test_each_class_has_distinct_glyph(self):
        glyphs = {
            account_glyph("Expenses:Food")[0],
            account_glyph("Income:Salary")[0],
            account_glyph("Assets:B:SPK")[0],
            account_glyph("Liabilities:CreditCard:Visa")[0],
            account_glyph("Equity:Opening-Balances")[0],
        }
        # Five classes, five distinct glyphs — visual orientation depends on it.
        assert len(glyphs) == 5

    def test_each_class_has_distinct_style(self):
        styles = {
            account_glyph("Expenses:Food")[1],
            account_glyph("Income:Salary")[1],
            account_glyph("Assets:B:SPK")[1],
            account_glyph("Liabilities:CreditCard:Visa")[1],
            account_glyph("Equity:Opening-Balances")[1],
        }
        assert len(styles) == 5

    def test_unknown_prefix_returns_neutral_marker(self):
        glyph, style = account_glyph("WeirdRoot:Sub")
        assert glyph == "\u00b7"  # mid-dot
        assert style == "white"

    def test_helper_is_pure(self):
        # Two calls with the same input return identical tuples.
        assert account_glyph("Expenses:Food") == account_glyph("Expenses:Food")

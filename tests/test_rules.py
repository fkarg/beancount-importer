from __future__ import annotations

import json
from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest

from beancount_importer.models import SourceTransaction
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.engine import find_matching_rule
from beancount_importer.rules.storage import load_rules, save_rules


def make_txn(
    payee: str = "Netflix",
    description: str = "Netflix Abo",
    amount: str = "-15.99",
    bank_key: str = "spk",
) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 1, 15),
        amount=Decimal(amount),
        currency="EUR",
        payee=payee,
        description=description,
        bank_key=bank_key,
    )


def make_rule(**kwargs) -> CategorizationRule:
    defaults = dict(target_account="Expenses:Entertainment")
    return CategorizationRule(**(defaults | kwargs))


# ── CategorizationRule.matches ───────────────────────────────────────────────

class TestCategorizationRuleMatches:
    def test_matches_payee(self):
        rule = make_rule(payee_pattern="Netflix")
        assert rule.matches(make_txn(payee="Netflix"))

    def test_no_match_wrong_payee(self):
        rule = make_rule(payee_pattern="Netflix")
        assert not rule.matches(make_txn(payee="Spotify"))

    def test_payee_regex_partial(self):
        rule = make_rule(payee_pattern="net")
        assert rule.matches(make_txn(payee="Netflix"))

    def test_payee_regex_case_insensitive(self):
        rule = make_rule(payee_pattern="NETFLIX")
        assert rule.matches(make_txn(payee="netflix"))

    def test_matches_description(self):
        rule = make_rule(description_pattern="Abo")
        assert rule.matches(make_txn(description="Netflix Abo"))

    def test_no_match_wrong_description(self):
        rule = make_rule(description_pattern="Premium")
        assert not rule.matches(make_txn(description="Netflix Abo"))

    def test_amount_sign_debit_matches_negative(self):
        rule = make_rule(amount_sign="debit")
        assert rule.matches(make_txn(amount="-15.99"))

    def test_amount_sign_debit_rejects_positive(self):
        rule = make_rule(amount_sign="debit")
        assert not rule.matches(make_txn(amount="100.00"))

    def test_amount_sign_credit_matches_positive(self):
        rule = make_rule(amount_sign="credit")
        assert rule.matches(make_txn(amount="100.00"))

    def test_amount_sign_credit_rejects_negative(self):
        rule = make_rule(amount_sign="credit")
        assert not rule.matches(make_txn(amount="-15.99"))

    def test_bank_key_filter(self):
        rule = make_rule(bank_key="n26")
        assert not rule.matches(make_txn(bank_key="spk"))
        assert rule.matches(make_txn(bank_key="n26"))

    def test_no_filters_matches_all(self):
        rule = make_rule()
        assert rule.matches(make_txn())

    def test_combined_payee_and_description(self):
        rule = make_rule(payee_pattern="Netflix", description_pattern="Abo")
        assert rule.matches(make_txn(payee="Netflix", description="Netflix Abo"))
        assert not rule.matches(make_txn(payee="Netflix", description="Premium"))

    def test_match_any_matches_when_either_field_hits(self):
        # `match_any` turns the two patterns into an OR — one rule replaces a
        # payee-rule + description-rule pair.
        rule = make_rule(
            payee_pattern="REWE", description_pattern="REWE", match_any=True
        )
        assert rule.matches(make_txn(payee="REWE Markt", description="card 123"))
        assert rule.matches(make_txn(payee="unknown", description="REWE Filiale"))
        assert not rule.matches(make_txn(payee="Aldi", description="card 123"))

    def test_match_any_defaults_off_keeps_and_semantics(self):
        assert make_rule().match_any is False
        rule = make_rule(payee_pattern="Netflix", description_pattern="Abo")
        # AND (default): both must hit.
        assert not rule.matches(make_txn(payee="Netflix", description="Premium"))

    def test_invalid_regex_raises(self):
        with pytest.raises(ValueError):
            make_rule(payee_pattern="[invalid")

    def test_default_match_mode_is_regex(self):
        # Backward-compat: rules loaded without match_mode keep regex semantics.
        assert make_rule().match_mode == "regex"

    def test_contains_matches_literal_substring_with_specials(self):
        # A `contains` pattern is a literal — regex metacharacters are matched
        # verbatim, not interpreted. `DE*RT4` would fail as a regex here.
        rule = make_rule(payee_pattern="DE*RT4", match_mode="contains")
        assert rule.matches(make_txn(payee="AMZN MKTP DE*RT4..."))
        assert not rule.matches(make_txn(payee="AMZN MKTP DERT4"))

    def test_contains_is_case_insensitive(self):
        rule = make_rule(payee_pattern="mktp", match_mode="contains")
        assert rule.matches(make_txn(payee="AMZN MKTP DE"))

    def test_exact_requires_whole_string(self):
        rule = make_rule(payee_pattern="Netflix", match_mode="exact")
        assert rule.matches(make_txn(payee="netflix"))  # casefold equality
        assert not rule.matches(make_txn(payee="Netflix Abo"))

    def test_contains_pattern_skips_regex_validation(self):
        # A literal `contains` pattern may contain regex-invalid text; it must
        # not be rejected by the regex validator.
        rule = make_rule(payee_pattern="[invalid", match_mode="contains")
        assert rule.matches(make_txn(payee="x [invalid y"))

    def test_regex_mode_still_validates(self):
        with pytest.raises(ValueError):
            make_rule(payee_pattern="[invalid", match_mode="regex")


class TestCategorizationRuleSuppressionFlags:
    def test_defaults_off(self):
        rule = make_rule()
        assert rule.suppress_updates is False
        assert rule.suppress_payee_updates is False
        assert rule.suppress_narration_updates is False
        assert rule.suppress_account_updates is False

    def test_can_enable(self):
        rule = make_rule(suppress_updates=True)
        assert rule.suppress_updates is True


class TestCategorizationRuleTransformFields:
    def test_defaults(self):
        rule = make_rule()
        assert rule.settle_days is None
        assert rule.add_actual_date is False
        assert rule.amortize_months is None
        assert rule.amortize_type == ""

    def test_settle_days(self):
        rule = make_rule(settle_days=3)
        assert rule.settle_days == 3

    def test_amortize(self):
        rule = make_rule(amortize_months=12, amortize_type="prepaid_months")
        assert rule.amortize_months == 12
        assert rule.amortize_type == "prepaid_months"

    def test_payee_none_with_pattern(self):
        rule = make_rule(payee_pattern="Netflix")
        txn = SourceTransaction(
            booking_date=date(2024, 1, 15),
            amount=Decimal("-10"),
            currency="EUR",
            payee=None,
            bank_key="spk",
        )
        assert not rule.matches(txn)


# ── find_matching_rule ───────────────────────────────────────────────────────

class TestFindMatchingRule:
    def test_returns_first_match(self):
        rules = [
            make_rule(payee_pattern="Netflix", target_account="Expenses:Entertainment"),
            make_rule(payee_pattern="Netflix", target_account="Expenses:Other"),
        ]
        result = find_matching_rule(make_txn(), rules)
        assert result is not None
        assert result.target_account == "Expenses:Entertainment"

    def test_returns_none_when_no_match(self):
        rules = [make_rule(payee_pattern="Spotify")]
        result = find_matching_rule(make_txn(payee="Netflix"), rules)
        assert result is None

    def test_returns_none_for_empty_rules(self):
        assert find_matching_rule(make_txn(), []) is None

    def test_skips_non_matching(self):
        rules = [
            make_rule(payee_pattern="Spotify", target_account="Expenses:Music"),
            make_rule(payee_pattern="Netflix", target_account="Expenses:Entertainment"),
        ]
        result = find_matching_rule(make_txn(payee="Netflix"), rules)
        assert result is not None
        assert result.target_account == "Expenses:Entertainment"


# ── storage: load/save ───────────────────────────────────────────────────────

class TestRulesStorage:
    def test_load_missing_returns_empty(self, tmp_path: Path):
        rules = load_rules(tmp_path / "nonexistent.json")
        assert rules == []

    def test_roundtrip(self, tmp_path: Path):
        rules = [
            CategorizationRule(
                target_account="Expenses:Entertainment",
                payee_pattern="Netflix",
                amount_sign="debit",
            )
        ]
        path = tmp_path / "rules.json"
        save_rules(rules, path)
        loaded = load_rules(path)
        assert len(loaded) == 1
        assert loaded[0].target_account == "Expenses:Entertainment"
        assert loaded[0].payee_pattern == "Netflix"
        assert loaded[0].amount_sign == "debit"

    def test_saved_file_is_valid_json(self, tmp_path: Path):
        rules = [make_rule(payee_pattern="Rewe", target_account="Expenses:Groceries")]
        path = tmp_path / "rules.json"
        save_rules(rules, path)
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_save_omits_default_and_null_fields(self, tmp_path: Path):
        # Rules carry many fields that are usually at their default; writing
        # them all bloats the file. Only non-default fields are serialized.
        path = tmp_path / "rules.json"
        save_rules(
            [CategorizationRule(
                target_account="Expenses:X",
                payee_pattern="Y",
                match_mode="contains",
            )],
            path,
        )
        assert json.loads(path.read_text()) == [
            {"target_account": "Expenses:X", "payee_pattern": "Y",
             "match_mode": "contains"}
        ]
        # Missing fields still load as their defaults.
        loaded = load_rules(path)[0]
        assert loaded.override_payee is None
        assert loaded.amount_sign == ""
        assert loaded.match_any is False

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "subdir" / "rules.json"
        save_rules([], path)
        assert path.exists()

    def test_empty_rules_roundtrip(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        save_rules([], path)
        assert load_rules(path) == []

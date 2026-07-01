"""Rule hygiene analysis — static shadow detection over literal rules."""

from __future__ import annotations

from beancount_importer.rules.doctor import (
    _describe,
    _single_field,
    analyze_rules,
    format_report,
)
from beancount_importer.rules.models import CategorizationRule


def _rule(payee="", desc="", mode="contains", bank="", sign="", target="Expenses:X"):
    return CategorizationRule(
        target_account=target,
        payee_pattern=payee,
        description_pattern=desc,
        match_mode=mode,
        bank_key=bank,
        amount_sign=sign,
    )


class TestShadowDetection:
    def test_broad_earlier_shadows_narrow_later(self):
        rules = [_rule(payee="REWE"), _rule(payee="REWE Filiale Berlin")]
        report = analyze_rules(rules)
        assert report.shadows == ((1, 0),)  # rule 1 unreachable, shadowed by 0

    def test_narrow_earlier_does_not_shadow_broad_later(self):
        # Specialization-first is legitimate: the narrow rule wins for its
        # subset, the broad rule still fires for everything else.
        rules = [_rule(payee="REWE Filiale Berlin"), _rule(payee="REWE")]
        assert analyze_rules(rules).shadows == ()

    def test_different_bank_is_not_a_shadow(self):
        rules = [_rule(payee="REWE", bank="spk"), _rule(payee="REWE Filiale", bank="n26")]
        assert analyze_rules(rules).shadows == ()

    def test_broad_any_bank_shadows_bank_scoped(self):
        # Earlier rule with no bank filter is broader → shadows a later
        # bank-scoped rule with a narrower pattern.
        rules = [_rule(payee="REWE"), _rule(payee="REWE Filiale", bank="spk")]
        assert analyze_rules(rules).shadows == ((1, 0),)

    def test_different_field_is_not_a_shadow(self):
        rules = [_rule(payee="ACME"), _rule(desc="ACME")]
        assert analyze_rules(rules).shadows == ()

    def test_exact_equal_shadows(self):
        rules = [_rule(payee="Netflix", mode="exact"), _rule(payee="netflix", mode="exact")]
        assert analyze_rules(rules).shadows == ((1, 0),)

    def test_contains_shadows_exact_when_substring(self):
        rules = [_rule(payee="AMZN"), _rule(payee="AMZN MKTP", mode="exact")]
        assert analyze_rules(rules).shadows == ((1, 0),)

    def test_exact_does_not_shadow_contains(self):
        rules = [_rule(payee="AMZN", mode="exact"), _rule(payee="AMZN")]
        assert analyze_rules(rules).shadows == ()

    def test_first_shadower_wins_no_duplicates(self):
        rules = [_rule(payee="RE"), _rule(payee="REW"), _rule(payee="REWE")]
        # rule 2 is shadowed — reported once, against the earliest shadower.
        shadows = analyze_rules(rules).shadows
        assert (2, 0) in shadows
        assert sum(1 for s in shadows if s[0] == 2) == 1


class TestConservativeCases:
    def test_amount_sign_mismatch_blocks_shadow(self):
        rules = [_rule(payee="RE", sign="debit"), _rule(payee="REWE", sign="credit")]
        assert analyze_rules(rules).shadows == ()

    def test_multi_field_rule_is_not_single_field(self):
        assert _single_field(_rule(payee="A", desc="B")) is None

    def test_multi_field_rule_never_shadows(self):
        # A rule with both patterns set is skipped (can't cheaply decide).
        rules = [_rule(payee="A", desc="B"), _rule(payee="A")]
        assert analyze_rules(rules).shadows == ()

    def test_describe_multi_field_falls_back_to_target(self):
        assert "Expenses:X" in _describe(_rule(payee="A", desc="B", target="Expenses:X"))


class TestManualFlagging:
    def test_genuine_regex_flagged_manual_not_shadowed(self):
        rules = [_rule(payee="AMZN.*", mode="regex"), _rule(payee="AMZN MKTP")]
        report = analyze_rules(rules)
        # A regex with metacharacters can't be statically decided → flagged.
        assert 0 in report.manual
        assert report.shadows == ()

    def test_plain_literal_regex_is_analyzed_not_manual(self):
        # A regex pattern with no metacharacters == a contains literal, so the
        # doctor analyzes it (and can catch it shadowing) instead of punting.
        rules = [
            _rule(payee="REWE", mode="regex"),
            _rule(payee="REWE Filiale", mode="regex"),
        ]
        report = analyze_rules(rules)
        assert report.manual == ()
        assert report.shadows == ((1, 0),)


class TestFormatReport:
    def test_reports_clean_when_nothing_found(self):
        lines = format_report([_rule(payee="A"), _rule(payee="B")], analyze_rules(
            [_rule(payee="A"), _rule(payee="B")]))
        assert any("no shadowed rules" in ln.lower() for ln in lines)

    def test_lists_shadow_with_both_patterns(self):
        rules = [_rule(payee="REWE"), _rule(payee="REWE Filiale")]
        lines = format_report(rules, analyze_rules(rules))
        blob = "\n".join(lines)
        assert "REWE" in blob and "REWE Filiale" in blob

    def test_lists_regex_rules_as_not_analyzed(self):
        rules = [_rule(payee="AMZN.*", mode="regex")]
        lines = format_report(rules, analyze_rules(rules))
        assert any("NOT ANALYZED" in ln for ln in lines)

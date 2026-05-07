from datetime import date
from decimal import Decimal

import pytest

from beancount_importer.models import CategoryProposal, Posting, SourceTransaction
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.transforms import (
    apply_transforms,
    load_transforms,
)
from beancount_importer.transforms.actual import ActualHook
from beancount_importer.transforms.amortize import AmortizeHook
from beancount_importer.transforms.settle import SettleHook


def make_txn(**kwargs) -> SourceTransaction:
    defaults = dict(
        booking_date=date(2024, 6, 10),
        amount=Decimal("-100.00"),
        currency="EUR",
        bank_key="spk",
    )
    return SourceTransaction(**(defaults | kwargs))


def make_proposal(**kwargs) -> CategoryProposal:
    defaults = dict(
        action="categorize",
        postings=(Posting(account="Expenses:Foo"),),
    )
    return CategoryProposal(**(defaults | kwargs))


def make_rule(**kwargs) -> CategorizationRule:
    defaults = dict(target_account="Expenses:Foo")
    return CategorizationRule(**(defaults | kwargs))


class TestSettleHook:
    def test_applies_to_positive_settle_days(self):
        assert SettleHook().applies_to(make_rule(settle_days=3))

    def test_does_not_apply_to_negative(self):
        assert not SettleHook().applies_to(make_rule(settle_days=-2))

    def test_does_not_apply_when_unset(self):
        assert not SettleHook().applies_to(make_rule())

    def test_does_not_apply_to_zero(self):
        assert not SettleHook().applies_to(make_rule(settle_days=0))

    def test_adds_settle_metadata(self):
        rule = make_rule(settle_days=3)
        proposal = SettleHook().apply(make_proposal(), make_txn(), rule)
        assert proposal.metadata["settle"] == "2024-06-13"

    def test_preserves_existing_metadata(self):
        rule = make_rule(settle_days=3)
        proposal = make_proposal(metadata={"foo": "bar"})
        out = SettleHook().apply(proposal, make_txn(), rule)
        assert out.metadata == {"foo": "bar", "settle": "2024-06-13"}


class TestActualHook:
    def test_applies_to_negative_settle_days(self):
        assert ActualHook().applies_to(make_rule(settle_days=-2))

    def test_applies_to_add_actual_date(self):
        assert ActualHook().applies_to(make_rule(add_actual_date=True))

    def test_does_not_apply_to_positive_settle(self):
        assert not ActualHook().applies_to(make_rule(settle_days=3))

    def test_does_not_apply_when_neither(self):
        assert not ActualHook().applies_to(make_rule())

    def test_negative_settle_sets_actual(self):
        rule = make_rule(settle_days=-2)
        proposal = ActualHook().apply(make_proposal(), make_txn(), rule)
        assert proposal.metadata["actual"] == "2024-06-08"

    def test_extracted_date_promoted_when_earlier(self):
        rule = make_rule(add_actual_date=True)
        proposal = make_proposal(metadata={"_extracted_date": "2024-06-05"})
        out = ActualHook().apply(proposal, make_txn(), rule)
        assert out.metadata["actual"] == "2024-06-05"
        assert "_extracted_date" not in out.metadata

    def test_extracted_date_ignored_when_not_earlier(self):
        rule = make_rule(add_actual_date=True)
        proposal = make_proposal(metadata={"_extracted_date": "2024-06-15"})
        out = ActualHook().apply(proposal, make_txn(), rule)
        assert "actual" not in out.metadata

    def test_extracted_date_invalid_format_ignored(self):
        rule = make_rule(add_actual_date=True)
        proposal = make_proposal(metadata={"_extracted_date": "not-a-date"})
        out = ActualHook().apply(proposal, make_txn(), rule)
        assert "actual" not in out.metadata

    def test_add_actual_date_without_extracted_metadata(self):
        # rule.add_actual_date=True but no `_extracted_date` in proposal.metadata
        # — apply() must short-circuit and return the proposal unchanged.
        rule = make_rule(add_actual_date=True)
        out = ActualHook().apply(make_proposal(), make_txn(), rule)
        assert out.metadata == {}

    def test_apply_called_without_applicable_rule_is_noop(self):
        # Defensive: if apply() is invoked on a rule where applies_to() would
        # return False (neither settle_days<0 nor add_actual_date), the hook
        # must leave the proposal alone rather than write spurious metadata.
        rule = make_rule()  # all transform fields default
        out = ActualHook().apply(make_proposal(), make_txn(), rule)
        assert out.metadata == {}


class TestAmortizeHook:
    def test_applies_when_both_set(self):
        assert AmortizeHook().applies_to(
            make_rule(amortize_type="prepaid_months", amortize_months=12)
        )

    def test_does_not_apply_when_type_missing(self):
        assert not AmortizeHook().applies_to(make_rule(amortize_months=12))

    def test_does_not_apply_when_months_missing(self):
        assert not AmortizeHook().applies_to(make_rule(amortize_type="prepaid_months"))

    def test_uses_amortize_type_as_metadata_key(self):
        rule = make_rule(amortize_type="prepaid_months", amortize_months=12)
        out = AmortizeHook().apply(make_proposal(), make_txn(), rule)
        assert out.metadata["prepaid_months"] == "12"

    def test_lifetime_months(self):
        rule = make_rule(amortize_type="lifetime_months", amortize_months=24)
        out = AmortizeHook().apply(make_proposal(), make_txn(), rule)
        assert out.metadata["lifetime_months"] == "24"


class TestApplyTransforms:
    def test_runs_only_applicable_hooks(self):
        rule = make_rule(settle_days=3)
        hooks = [SettleHook(), ActualHook(), AmortizeHook()]
        out = apply_transforms(hooks, make_proposal(), make_txn(), rule)
        assert "settle" in out.metadata
        assert "actual" not in out.metadata

    def test_composes_multiple(self):
        rule = make_rule(settle_days=3, amortize_type="prepaid_months", amortize_months=6)
        hooks = [SettleHook(), AmortizeHook()]
        out = apply_transforms(hooks, make_proposal(), make_txn(), rule)
        assert out.metadata["settle"] == "2024-06-13"
        assert out.metadata["prepaid_months"] == "6"

    def test_no_hooks_passes_proposal_through(self):
        out = apply_transforms([], make_proposal(), make_txn(), make_rule())
        assert out == make_proposal()


class TestLoadTransforms:
    def test_loads_real_modules(self):
        hooks = load_transforms([
            "beancount_importer.transforms.settle",
            "beancount_importer.transforms.actual",
            "beancount_importer.transforms.amortize",
        ])
        assert len(hooks) == 3
        names = [h.name for h in hooks]
        assert names == ["settle", "actual", "amortize"]

    def test_missing_hook_attr_raises(self, tmp_path):
        bad = tmp_path / "bad_transform.py"
        bad.write_text("x = 1\n")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            with pytest.raises(TypeError):
                load_transforms(["bad_transform"])
        finally:
            sys.path.remove(str(tmp_path))

    def test_hook_not_satisfying_protocol_raises(self, tmp_path):
        # A module with a top-level `hook` attribute that is structurally
        # incompatible with TransformHook (no `apply` / `applies_to`) must
        # fail loudly at session start, not silently mis-route.
        bad = tmp_path / "wrong_hook.py"
        bad.write_text("hook = 'not a hook'\n")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            with pytest.raises(TypeError, match="does not satisfy TransformHook"):
                load_transforms(["wrong_hook"])
        finally:
            sys.path.remove(str(tmp_path))

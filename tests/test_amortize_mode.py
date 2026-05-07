"""Interactive amortize mode helpers."""

from __future__ import annotations

import pytest

from beancount_importer.cli import _augment_with_amortize
from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.transforms.amortize import (
    AMORTIZE_TYPES,
    amortize_metadata,
)


def _proposal(metadata: dict[str, str] | None = None) -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account="Expenses:Software"),),
        metadata=metadata or {},
    )


class TestAmortizeMetadata:
    def test_builds_typed_pair(self):
        assert amortize_metadata("amortize_months", 12) == {"amortize_months": "12"}
        assert amortize_metadata("prepaid_months", 6) == {"prepaid_months": "6"}

    def test_rejects_unknown_type(self):
        with pytest.raises(ValueError, match="unknown amortize_type"):
            amortize_metadata("foo_months", 12)

    def test_rejects_non_positive_months(self):
        with pytest.raises(ValueError, match="months must be >= 1"):
            amortize_metadata("amortize_months", 0)
        with pytest.raises(ValueError, match="months must be >= 1"):
            amortize_metadata("amortize_months", -1)

    def test_known_types_are_what_the_plugin_expects(self):
        # The plugin reads metadata key == amortize_type. If this changes,
        # ledgers stop being recognised at load time.
        assert AMORTIZE_TYPES == (
            "lifetime_months",
            "prepaid_months",
            "amortize_months",
        )


class TestAugmentWithAmortize:
    def test_merges_into_existing_metadata(self):
        original = _proposal({"document": "invoice.pdf"})
        augmented = _augment_with_amortize(original, "amortize_months", 12)
        assert augmented.metadata == {
            "document": "invoice.pdf",
            "amortize_months": "12",
        }
        # Original is unchanged (frozen model copy).
        assert original.metadata == {"document": "invoice.pdf"}

    def test_preserves_other_proposal_fields(self):
        original = _proposal()
        augmented = _augment_with_amortize(original, "prepaid_months", 24)
        assert augmented.action == original.action
        assert augmented.postings == original.postings

    def test_invalid_input_raises(self):
        with pytest.raises(ValueError):
            _augment_with_amortize(_proposal(), "bogus", 12)

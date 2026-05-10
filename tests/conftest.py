"""Shared fixtures for the test suite.

`deterministic_categorize` is the canonical `CategorizeFn` test double — it
ignores the context and always proposes `Expenses:Unknown` with the txn's own
payee/description carried through. New pipeline tests should prefer it over
inline lambdas; reach for a custom stub only when a test specifically needs
to assert on the context (e.g. near-misses, account hints, active tag).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import CategorizeContext, CategorizeFn

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def deterministic_categorize() -> CategorizeFn:
    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:Unknown"),),
            payee=ctx.txn.payee,
            narration=ctx.txn.description,
        )

    return _fn

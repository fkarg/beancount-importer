"""Settle-date transform.

When a rule sets `settle_days > 0`, mark the proposal with a `settle:` metadata
key carrying the booking date plus the offset. This represents the date the
funds will actually clear (vs. the booking date, which is when the txn appears
on the statement).

Negative settle_days (card-swipe before booking) is handled by `actual.py`,
not here — keeping each hook focused on one metadata key.
"""

from __future__ import annotations

from datetime import timedelta

from beancount_importer.models import CategoryProposal, SourceTransaction
from beancount_importer.rules.models import CategorizationRule


class SettleHook:
    name = "settle"

    def applies_to(self, rule: CategorizationRule) -> bool:
        return rule.settle_days is not None and rule.settle_days > 0

    def apply(
        self,
        proposal: CategoryProposal,
        txn: SourceTransaction,
        rule: CategorizationRule,
    ) -> CategoryProposal:
        assert rule.settle_days is not None and rule.settle_days > 0
        settle_date = txn.booking_date + timedelta(days=rule.settle_days)
        new_metadata = {**proposal.metadata, "settle": settle_date.isoformat()}
        return proposal.model_copy(update={"metadata": new_metadata})


hook = SettleHook()

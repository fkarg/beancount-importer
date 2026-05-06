"""Amortize transform.

Adds the `<amortize_type>: <months>` metadata pair that the
`beancount-plugins/amortize` plugin reads to spread an expense across multiple
periods.

The metadata *key* is the `amortize_type` itself (`lifetime_months`,
`prepaid_months`, or `amortize_months`) — that's the convention the plugin uses;
the value is the number of months as a string.
"""

from __future__ import annotations

from beancount_importer.models import CategoryProposal, SourceTransaction
from beancount_importer.rules.models import CategorizationRule


class AmortizeHook:
    name = "amortize"

    def applies_to(self, rule: CategorizationRule) -> bool:
        return bool(rule.amortize_type) and rule.amortize_months is not None

    def apply(
        self,
        proposal: CategoryProposal,
        txn: SourceTransaction,
        rule: CategorizationRule,
    ) -> CategoryProposal:
        assert rule.amortize_type and rule.amortize_months is not None
        new_metadata = {**proposal.metadata, rule.amortize_type: str(rule.amortize_months)}
        return proposal.model_copy(update={"metadata": new_metadata})


hook = AmortizeHook()

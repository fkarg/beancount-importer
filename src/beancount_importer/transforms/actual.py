"""Actual-date transform.

Adds an `actual:` metadata key recording the card-swipe date when it differs
from the booking date. Two triggers:

1. `rule.settle_days < 0`: rule explicitly says the swipe was N days before
   booking (mirror of settle.py for negative offsets).
2. `rule.add_actual_date=True`: rule asks for actual-date detection without a
   fixed offset. The pipeline supplies a candidate date via the proposal's
   metadata under the key `_extracted_date` (set during scoring/extraction);
   if present and earlier than booking, it's promoted to `actual:`.

The `_extracted_date` indirection keeps this hook pure — it doesn't run regex
extraction itself; the matching layer does that and stashes a candidate.
"""

from __future__ import annotations

from datetime import date, timedelta

from beancount_importer.models import CategoryProposal, SourceTransaction
from beancount_importer.rules.models import CategorizationRule


class ActualHook:
    name = "actual"

    def applies_to(self, rule: CategorizationRule) -> bool:
        if rule.settle_days is not None and rule.settle_days < 0:
            return True
        return rule.add_actual_date

    def apply(
        self,
        proposal: CategoryProposal,
        txn: SourceTransaction,
        rule: CategorizationRule,
    ) -> CategoryProposal:
        actual_date: date | None = None

        if rule.settle_days is not None and rule.settle_days < 0:
            actual_date = txn.booking_date + timedelta(days=rule.settle_days)
        elif rule.add_actual_date:
            extracted = proposal.metadata.get("_extracted_date")
            if extracted:
                try:
                    parsed = date.fromisoformat(extracted)
                    if parsed < txn.booking_date:
                        actual_date = parsed
                except ValueError:
                    pass

        if actual_date is None:
            return proposal

        new_metadata = {
            k: v for k, v in proposal.metadata.items() if k != "_extracted_date"
        }
        new_metadata["actual"] = actual_date.isoformat()
        return proposal.model_copy(update={"metadata": new_metadata})


hook = ActualHook()

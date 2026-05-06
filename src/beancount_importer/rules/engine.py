from __future__ import annotations

from beancount_importer.models import SourceTransaction
from beancount_importer.rules.models import CategorizationRule


def find_matching_rule(
    txn: SourceTransaction,
    rules: list[CategorizationRule],
) -> CategorizationRule | None:
    """Return the first rule that matches txn, or None."""
    for rule in rules:
        if rule.matches(txn):
            return rule
    return None

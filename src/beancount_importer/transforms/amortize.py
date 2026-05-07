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


# Valid amortize_type values, matching the keys the upstream plugin recognises.
AMORTIZE_TYPES: tuple[str, ...] = ("lifetime_months", "prepaid_months", "amortize_months")


def amortize_metadata(amortize_type: str, months: int) -> dict[str, str]:
    """Build the `<type>: <months>` metadata pair as a dict.

    Used for one-off interactive amortizations where there's no rule to
    drive `AmortizeHook` — the categorizer stamps this directly onto a
    proposal's metadata so it flows to output via `_format_new_entry`.
    Raises `ValueError` on an unknown type or non-positive months so a
    typo at the prompt doesn't silently produce garbage in the ledger.
    """
    if amortize_type not in AMORTIZE_TYPES:
        raise ValueError(
            f"unknown amortize_type {amortize_type!r}; expected one of {AMORTIZE_TYPES}"
        )
    if months < 1:
        raise ValueError(f"months must be >= 1, got {months}")
    return {amortize_type: str(months)}

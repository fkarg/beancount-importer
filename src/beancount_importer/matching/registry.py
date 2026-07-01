"""Cross-source matcher registry.

A matcher inspects a CSV row against the union of CSV rows across banks plus
existing ledger entries, and emits a `MatchOutcome` (or None) when it finds
that the row is already accounted for elsewhere.

Three outcome kinds today:

- `skip`: the row is a duplicate of something already booked. The pipeline
  drops it without prompting.
- `rewrite_target`: the row is real, but its target account isn't what the
  generic categorizer would pick — e.g. a PayPal-funded SPK debit should
  book to `Assets:B:PayPal`, not the merchant. The pipeline replaces the
  proposal with the matcher-provided account + metadata.
- `link_placeholder`: the row completes a `via_paypal: TRUE` placeholder
  entry (`matched_entry` must be set). The pipeline rewrites that entry in
  place — upgrading the marker to posting-level `paypal: <date>` — and
  consumes the row without prompting.

Hooks live in modules listed in `MatchingConfig.enabled_matchers`. Each
module exposes a top-level `hook` object satisfying the `MatcherHook`
protocol — same pattern as `transforms/`. Order matters: the first hook
returning a non-None outcome wins.
"""

from __future__ import annotations

import importlib
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from beancount_importer.models import LedgerEntry, SourceTransaction


class MatchOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    kind: Literal["skip", "rewrite_target", "link_placeholder"]
    reason: str
    target_account: str | None = None
    metadata: dict[str, str] = {}
    matched_entry: LedgerEntry | None = None
    matched_txn: SourceTransaction | None = None


@runtime_checkable
class MatcherHook(Protocol):
    name: str

    def match(
        self,
        txn: SourceTransaction,
        all_csv_by_bank: dict[str, list[SourceTransaction]],
        existing_entries: list[LedgerEntry],
    ) -> MatchOutcome | None: ...


def load_matchers(module_paths: list[str]) -> list[MatcherHook]:
    """Import each module path and collect its top-level `hook` attribute.

    Mirrors `transforms.load_transforms`: validates the structural protocol
    on load so misconfiguration fails fast.
    """
    hooks: list[MatcherHook] = []
    for path in module_paths:
        module = importlib.import_module(path)
        hook = getattr(module, "hook", None)
        if hook is None:
            raise TypeError(f"Matcher module {path!r} has no top-level `hook`")
        if not isinstance(hook, MatcherHook):
            raise TypeError(f"Matcher {path!r}.hook does not satisfy MatcherHook")
        hooks.append(hook)
    return hooks


def first_outcome(
    hooks: list[MatcherHook],
    txn: SourceTransaction,
    all_csv_by_bank: dict[str, list[SourceTransaction]],
    existing_entries: list[LedgerEntry],
) -> MatchOutcome | None:
    """Return the first non-None outcome; later hooks don't run."""
    for hook in hooks:
        outcome = hook.match(txn, all_csv_by_bank, existing_entries)
        if outcome is not None:
            return outcome
    return None

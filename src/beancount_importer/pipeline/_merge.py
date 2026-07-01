"""Translating a Screen-3 merge decision into a finalised `ImportResult`.

`_apply_merge_decision` is the bulk of this module — a six-branch
dispatch on the `MergeDecision.action` field that rewrites the result
the pipeline auto-built into the form the user actually wants
(keep / skip / quit / import_new / block, with `update` as passthrough).

Lives outside run.py because:
- The dispatch is long and self-contained (no pipeline state threading).
- Both helpers (`_proposal_from_entry`, `_block_update_rule`) are
  callable directly by tests poking the merge code path.
"""

from __future__ import annotations

from beancount_importer.config import BankConfig
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    SourceTransaction,
)
from beancount_importer.pipeline._result import _format_new_entry
from beancount_importer.pipeline.types import MergeDecision
from beancount_importer.rules.models import CategorizationRule


def _apply_merge_decision(
    result: ImportResult,
    decision: MergeDecision,
    txn: SourceTransaction,
    bank: BankConfig,
    working_rules: list[CategorizationRule],
    *,
    narration_max_length: int | None = None,
) -> tuple[ImportResult, list[CategorizationRule]]:
    """Translate a Screen-3 outcome into a finalised `ImportResult`.

    `working_rules` may grow when the user picks `block` (we install a
    `suppress_updates` rule so future runs auto-skip). Decisions that
    don't touch rules return `working_rules` unchanged.
    """
    assert result.matched_entry is not None  # gated by the caller
    assert result.proposal is not None
    entry = result.matched_entry

    if decision.action == "update":
        # Default: keep the auto-generated update result as-is.
        return result, working_rules

    if decision.action == "keep":
        # Silent match — record a proposal that mirrors the existing
        # entry so replay reproduces the same empty-diff outcome next
        # run, without re-prompting.
        mirror = _proposal_from_entry(entry)
        kept = result.model_copy(
            update={
                "action": "update",
                "proposed_changes": [],
                "proposal": mirror,
                "skip_reason": "user_kept",
            }
        )
        return kept, working_rules

    if decision.action == "skip":
        return (
            result.model_copy(
                update={
                    "action": "skip",
                    "matched_entry": None,
                    "proposed_changes": [],
                    "proposal": None,
                    "skip_reason": "user_skipped",
                }
            ),
            working_rules,
        )

    if decision.action == "quit":
        return (
            result.model_copy(
                update={
                    "action": "quit",
                    "matched_entry": None,
                    "proposed_changes": [],
                }
            ),
            working_rules,
        )

    if decision.action == "import_new":
        # Fresh entry instead of touching the matched one. The proposal
        # already came from the categorizer; we just reformat it as a
        # new-entry text and clear the matched-entry pointer.
        new_text = _format_new_entry(
            bank, txn, result.proposal, narration_max_length=narration_max_length
        )
        return (
            result.model_copy(
                update={
                    "action": "new",
                    "matched_entry": None,
                    "proposed_changes": [],
                    "new_entry_text": new_text,
                }
            ),
            working_rules,
        )

    # decision.action == "block" — install a skip-update rule for this
    # payee and skip the current row. Future runs match the rule and
    # produce a `skip_rule` result without ever reaching Screen 3.
    block_rule = _block_update_rule(txn, entry)
    return (
        result.model_copy(
            update={
                "action": "skip",
                "matched_entry": None,
                "proposed_changes": [],
                "proposal": None,
                "skip_reason": "user_blocked",
                "new_rule": block_rule,
            }
        ),
        [*working_rules, block_rule] if block_rule else working_rules,
    )


def _proposal_from_entry(entry: LedgerEntry) -> CategoryProposal:
    """Build a categorize proposal that exactly mirrors `entry`.

    Used for the Screen-3 `keep` branch: a proposal matching the existing
    entry produces an empty `_diff_changes` and replays as a silent
    skip on subsequent runs.
    """
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=entry.target_account),),
        payee=entry.payee,
        narration=entry.narration,
    )


def _block_update_rule(
    txn: SourceTransaction, entry: LedgerEntry
) -> CategorizationRule | None:
    """Synthesize a `suppress_updates=True` rule that matches `txn`.

    Prefers payee-based matching; falls back to description if payee is
    absent. Returns None when neither field is available — the caller
    treats this as "block didn't take" and downgrades to a plain skip.
    """
    if txn.payee:
        return CategorizationRule(
            target_account=entry.target_account,
            payee_pattern=txn.payee.strip(),
            match_mode="contains",
            bank_key=txn.bank_key,
            suppress_updates=True,
        )
    if txn.description:
        return CategorizationRule(
            target_account=entry.target_account,
            description_pattern=txn.description.strip(),
            match_mode="contains",
            bank_key=txn.bank_key,
            suppress_updates=True,
        )
    return None

"""Synthesizing `CategoryProposal`s from rules, matcher outcomes, and candidates.

Six small pure functions plus a regex-escape helper. Each one builds (or
declines to build) a proposal from a specific input source:

- `_proposal_from_outcome` — cross-source matcher rewrite_target
- `_proposal_from_rule`    — `CategorizationRule.target_account` + overrides
- `_seed_proposal`         — Screen-1 default (rule > top candidate)
- `_silent_skip_proposal`  — seed iff invoking the user would be a no-op
- `_derive_rule`           — synthesize a rule from a one-off user pick
- `_is_ambiguous_match`    — top two candidates within `min_delta`

Used by `_resolve_proposal` in run.py and by `_apply_merge_decision` in
`_merge.py`. Kept separate so the pipeline's flow code reads as flow.
"""

from __future__ import annotations

from beancount_importer.matching.registry import MatchOutcome
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    Posting,
    SourceTransaction,
)
from beancount_importer.pipeline._result import _diff_changes
from beancount_importer.rules.models import CategorizationRule


def _proposal_from_outcome(outcome: MatchOutcome) -> CategoryProposal:
    """Build a categorize proposal from a `rewrite_target` matcher outcome.

    The target account comes from the matcher; metadata is folded in verbatim.
    Payee/narration are left to the existing source transaction defaults so a
    later rule or user override can still tweak them.
    """
    assert outcome.target_account is not None
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=outcome.target_account),),
        metadata=dict(outcome.metadata),
    )


def _proposal_from_rule(rule: CategorizationRule) -> CategoryProposal:
    return CategoryProposal(
        action="categorize",
        postings=(Posting(account=rule.target_account),),
        payee=rule.override_payee,
        narration=rule.override_narration,
        tag=rule.tag,
        rule_used=rule,
    )


def _seed_proposal(
    rule: CategorizationRule | None,
    candidates: tuple[tuple[LedgerEntry, float], ...],
) -> CategoryProposal | None:
    """The auto-built proposal that would seed Screen 1 (or replace it).

    Rule wins over candidate — a user-authored override is more authoritative
    than fuzzy-match target reuse. Returns None when neither input has
    anything to contribute (Path B: fresh pick required).
    """
    if rule is not None:
        return _proposal_from_rule(rule)
    if candidates:
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=candidates[0][0].target_account),),
        )
    return None


def _is_ambiguous_match(
    candidates: tuple[tuple[LedgerEntry, float], ...],
    min_delta: float,
) -> bool:
    """Two or more candidates with the top scores within `min_delta`.

    Single-candidate hits are unambiguous by definition. Wide gaps (top
    decisively better) are also unambiguous — the user gets Screen 1 with
    the top entry's target reused.
    """
    if len(candidates) < 2:
        return False
    return (candidates[0][1] - candidates[1][1]) < min_delta


def _silent_skip_proposal(
    rule: CategorizationRule | None,
    candidates: tuple[tuple[LedgerEntry, float], ...],
    min_delta: float,
) -> CategoryProposal | None:
    """Return the seed proposal iff invoking categorize_fn would be a no-op.

    A "no-op" means the proposal produces zero `_diff_changes` against the
    relevant entry (or every ambiguous candidate, when the top two scores
    are within `min_delta`). The user gets nothing to consent to — the
    pipeline silent-skips the row.

    Returns None when there's a real choice: either no candidate to diff
    against (a fresh entry the user must confirm), or at least one
    candidate where the proposal would actually change a field.
    """
    if not candidates:
        # No entry to diff against — even a rule-driven new entry needs
        # user consent (Enter on Screen 1).
        return None

    seed = _seed_proposal(rule, candidates)
    if seed is None:  # pragma: no cover - candidates non-empty guarantees seed
        return None

    if rule is None and _is_ambiguous_match(candidates, min_delta):
        top_score = candidates[0][1]
        diff_targets = [
            entry for (entry, score) in candidates
            if (top_score - score) < min_delta
        ]
    else:
        diff_targets = [candidates[0][0]]

    if all(not _diff_changes(entry, seed, rule) for entry in diff_targets):
        return seed
    return None


def _derive_rule(
    txn: SourceTransaction, proposal: CategoryProposal
) -> CategorizationRule | None:
    """Synthesize a CategorizationRule from a one-off categorize proposal.

    Heuristic: prefer matching by payee when available, falling back to
    description. The pattern is the raw literal text matched case-insensitively
    as a substring (`match_mode="contains"`) — human-readable, no regex escapes;
    users can refine it (or switch to regex) later via the rule editor.
    """
    if not proposal.postings:
        return None
    target = proposal.postings[0].account
    payee_pattern = ""
    desc_pattern = ""
    if txn.payee:
        payee_pattern = txn.payee.strip()
    elif txn.description:
        desc_pattern = txn.description.strip()
    if not payee_pattern and not desc_pattern:
        return None
    return CategorizationRule(
        target_account=target,
        payee_pattern=payee_pattern,
        description_pattern=desc_pattern,
        match_mode="contains",
        # Any bank by default — most payee-based rules aren't bank-specific.
        bank_key="",
        override_payee=proposal.payee,
        override_narration=proposal.narration,
        tag=proposal.tag,
    )

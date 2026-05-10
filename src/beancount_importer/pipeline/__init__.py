"""Public pipeline API.

Splits across three sibling modules:
- `run`     — `pipeline.run()` and the per-transaction machinery
- `preview` — bean-side reverse-provenance stats for `--preview`
- `_shared` — parse + load helpers used by both

This `__init__` re-exports every name the original `pipeline.py`
surface exposed, so existing imports
(`from beancount_importer.pipeline import …`) keep working.

The few private helpers re-exported here (`_block_update_rule`,
`_is_ambiguous_match`, `_seed_proposal`) are referenced by tests via
this top-level import path; everything else stays scoped to the
relevant submodule.
"""

from __future__ import annotations

from beancount_importer.pipeline.preview import (
    BeanProvenanceStats,
    compute_bean_provenance_stats,
)
from beancount_importer.pipeline.run import (
    CategorizeContext,
    CategorizeFn,
    MergeContext,
    MergeDecision,
    MergeFn,
    NearMiss,
    NoopReporter,
    Reporter,
    _apply_rule_overrides,
    _block_update_rule,
    _compute_near_misses,
    _is_ambiguous_match,
    _seed_proposal,
    run,
)

__all__ = [
    "BeanProvenanceStats",
    "CategorizeContext",
    "CategorizeFn",
    "MergeContext",
    "MergeDecision",
    "MergeFn",
    "NearMiss",
    "NoopReporter",
    "Reporter",
    "_apply_rule_overrides",
    "_block_update_rule",
    "_compute_near_misses",
    "_is_ambiguous_match",
    "_seed_proposal",
    "compute_bean_provenance_stats",
    "run",
]

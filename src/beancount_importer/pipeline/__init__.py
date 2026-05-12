"""Public pipeline API.

Submodules:
- `types`     — public payload types (`Reporter`, `CategorizeContext`, …)
- `run`       — `pipeline.run()` and the per-transaction phase machinery
- `_result`   — building `ImportResult` from proposal + candidates
- `_proposal` — proposal synthesis helpers (rule/seed/silent-skip)
- `_merge`    — Screen-3 outcome translation
- `preview`   — bean-side reverse-provenance stats for `--preview`
- `_shared`   — parse + load helpers used by both
- `_clean`    — PayPal-noise pre-match cleaner

This `__init__` re-exports every name the original `pipeline.py`
surface exposed, so existing imports
(`from beancount_importer.pipeline import …`) keep working.

The private helpers re-exported here (`_apply_rule_overrides`,
`_compute_near_misses`, `_is_ambiguous_match`, `_seed_proposal`) are
referenced by tests via this top-level import path; everything else
stays scoped to the relevant submodule.
"""

from __future__ import annotations

from beancount_importer.pipeline._proposal import (
    _is_ambiguous_match,
    _seed_proposal,
)
from beancount_importer.pipeline.preview import (
    BeanProvenanceStats,
    compute_bean_provenance_stats,
)
from beancount_importer.pipeline.run import (
    _apply_rule_overrides,
    _compute_near_misses,
    run,
)
from beancount_importer.pipeline.types import (
    CategorizeContext,
    CategorizeFn,
    MergeContext,
    MergeDecision,
    MergeFn,
    NearMiss,
    NoopReporter,
    Reporter,
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
    "_compute_near_misses",
    "_is_ambiguous_match",
    "_seed_proposal",
    "compute_bean_provenance_stats",
    "run",
]

"""Public types in the pipeline surface.

Three audiences:
- The CLI host (cli.py / categorizer) imports `CategorizeFn`, `MergeFn`,
  `Reporter`, and the `*Context` payloads to plug into `pipeline.run()`.
- Test stubs implement `Reporter` (or use `NoopReporter`) and synthesize
  `CategorizeContext` / `MergeContext` for unit tests.
- The pipeline itself imports them to type its own signatures.

Kept in a dedicated module so the run-loop file stays focused on flow,
and so the public interface is findable without scrolling past 1500
lines of implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag, RememberedTag


class NearMiss(BaseModel):
    """A close-but-not-quite match surfaced for diagnostic display.

    Computed by the pipeline only when no real candidates land — exists
    purely to give the user a readable answer to "why is this row being
    prompted instead of dedup-skipped?".

    Two reasons:
    - `below_threshold`: same source-account bucket, scored under `min_score`.
      Usually means rule-cleaned narration drifted under the cutoff.
    - `different_bucket`: same currency + |amount| within date tolerance, but
      the entry's `source_account` doesn't match the txn's bank. Catches the
      sub-account case (entry on `Assets:B:SPK:Checking` while txn buckets
      to `Assets:B:SPK`) and other misfiled placements.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    entry: LedgerEntry
    score: float
    reason: Literal["below_threshold", "different_bucket"]


class CategorizeContext(BaseModel):
    """Inputs supplied to a CategorizeFn for one transaction.

    `existing_entries` is the full ledger universe across all banks; the
    categorizer can derive ranked account suggestions from it via
    `matching.account_suggest.rank_accounts`. `account_hints` is a
    pre-computed shortcut populated by the pipeline.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    txn: SourceTransaction
    rules: tuple[CategorizationRule, ...]
    candidates: tuple[tuple[LedgerEntry, float], ...] = ()
    matched_rule: CategorizationRule | None = None
    account_hints: tuple[str, ...] = ()
    active_tag: ActiveTag | None = None
    # Picker source for the `[t]` menu: interacted tags (with windows) unioned
    # with ledger tag names, grown in-session. Ordered most-relevant-first.
    known_tags: tuple[RememberedTag, ...] = ()
    existing_entries: tuple[LedgerEntry, ...] = ()
    # Source-side account (e.g. `Assets:B:SPK`) and run progress; needed by
    # the screen-driven categorizer to render the headline + state header.
    source_account: str = ""
    progress: tuple[int, int] = (0, 0)
    # Diagnostic-only: populated only when `candidates` is empty, surfaces
    # why the user is being prompted instead of seeing a silent skip.
    near_misses: tuple[NearMiss, ...] = ()
    # Pre-computed routing hints. The pipeline owns silent-skip detection
    # (zero-diff updates never reach categorize_fn) and ambiguity detection
    # (top two candidates within `min_delta`). The host uses these to pick
    # which screen to render without redoing the work.
    seed_proposal: CategoryProposal | None = None
    is_ambiguous: bool = False


CategorizeFn = Callable[[CategorizeContext], CategoryProposal]


class MergeContext(BaseModel):
    """Inputs supplied to a `MergeFn` when an `update` would change fields.

    Fires after `_build_result` decides this txn matches an existing entry
    AND the resulting `proposed_changes` is non-empty. Lets the host
    (cli.py) prompt the user via Screen 3 before the splice happens.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    txn: SourceTransaction
    proposal: CategoryProposal
    matched_entry: LedgerEntry
    proposed_changes: tuple[ProposedChange, ...]
    progress: tuple[int, int] = (0, 0)
    active_tag: ActiveTag | None = None


class MergeDecision(BaseModel):
    """Returned from a `MergeFn`. The pipeline routes on `action`:

    - `update`     → keep the auto-generated update result as-is
    - `keep`       → silent-match (no splice; replay reproduces silently)
    - `import_new` → create a fresh entry instead of updating the matched one
    - `block`      → install a `suppress_updates` rule and skip this row
    - `skip`       → no-op for this run; row reappears next run
    - `quit`       → tear down the run
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["update", "keep", "import_new", "block", "skip", "quit"]


MergeFn = Callable[[MergeContext], MergeDecision]


@runtime_checkable
class Reporter(Protocol):
    """Receives all user-visible output from the pipeline."""

    def on_result(self, result: ImportResult) -> None: ...
    def on_progress(
        self, current: int, total: int, bank: str, booking_date: date
    ) -> None: ...
    def on_error(self, message: str) -> None: ...


class NoopReporter:
    """Discard all events; useful in tests."""

    def on_result(self, result: ImportResult) -> None:
        del result

    def on_progress(
        self, current: int, total: int, bank: str, booking_date: date
    ) -> None:
        del current, total, bank, booking_date

    def on_error(self, message: str) -> None:
        del message

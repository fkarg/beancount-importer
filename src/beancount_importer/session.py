"""Session-level types: ImportOptions (CLI flags) and ImportSession (pipeline input).

Both are frozen Pydantic models. Mutable outcomes (new rules, replay entries,
tag-state deltas) flow back through ImportResult and are persisted by the CLI;
nothing in the session itself mutates during a run.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from beancount_importer.config import Config
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import TagState


class ImportOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    interactive: bool = True
    auto_update: bool = False
    auto_threshold: float | None = None  # score >= threshold → no prompt
    skip_existing: bool = False
    no_update: bool = False
    no_import: bool = False
    preview: bool = False
    dry_run: bool = False
    bank_filter: str | None = None
    # When set, only transactions whose booking_date.year is in this tuple are
    # processed. None means "no filter" — all parsed transactions are passed
    # through. The session's `year` still controls output-path templating.
    year_filter: tuple[int, ...] | None = None


class ImportSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    year: int
    config: Config
    rules: tuple[CategorizationRule, ...] = ()
    tag_state: TagState = TagState()
    options: ImportOptions = ImportOptions()

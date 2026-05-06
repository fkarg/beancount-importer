"""Active-tag state: stateful trip/event tagging that auto-applies to subsequent
transactions.

Three modes:
- `always`: tag every new transaction until cleared
- `once`:   tag the next transaction only, then auto-clear
- `duration`: tag transactions whose booking_date falls in [from_date, until_date]

Persistence lives in `.import_tag_state.json`, separate from rules and config —
state changes mid-session, rules and config are edited deliberately.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ActiveTag(BaseModel):
    model_config = ConfigDict(frozen=True)

    tag: str
    mode: Literal["always", "once", "duration"]
    from_date: date | None = None
    until_date: date | None = None

    def applies_to(self, booking_date: date) -> bool:
        """Whether this active tag should apply to a transaction on `booking_date`.

        `always` and `once` always apply (the pipeline auto-clears `once` after).
        `duration` applies only within [from_date, until_date]; either bound may be None.
        """
        if self.mode in ("always", "once"):
            return True
        if self.from_date is not None and booking_date < self.from_date:
            return False
        if self.until_date is not None and booking_date > self.until_date:
            return False
        return True

    def is_expired(self, booking_date: date) -> bool:
        """Whether `duration` mode has run past its until_date."""
        return (
            self.mode == "duration"
            and self.until_date is not None
            and booking_date > self.until_date
        )


class TagStateDelta(BaseModel):
    """Returned in ImportResult; the pipeline applies it before the next iteration."""

    model_config = ConfigDict(frozen=True)

    op: Literal["set", "clear", "noop"]
    new_state: ActiveTag | None = None  # required for op="set"


class TagState(BaseModel):
    """Persisted state. Loaded into ImportSession at start, written by CLI after run()."""

    model_config = ConfigDict(frozen=True)

    active: ActiveTag | None = None
    recent: tuple[str, ...] = ()  # LRU of recently used tags

    def with_recent(self, tag: str, cap: int = 10) -> TagState:
        """Return a new TagState with `tag` moved to the front of `recent`, capped."""
        deduped = tuple(t for t in self.recent if t != tag)
        return TagState(active=self.active, recent=(tag, *deduped)[:cap])

    def with_active(self, active: ActiveTag | None) -> TagState:
        return TagState(active=active, recent=self.recent)

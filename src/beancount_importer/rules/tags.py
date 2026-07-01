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
        return not (self.until_date is not None and booking_date > self.until_date)

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


class RememberedTag(BaseModel):
    """A tag the user has interacted with, remembered for the `[t]` picker.

    Beancount stores only the bare tag name, never the window the user chose,
    so we persist it here. `from_date`/`until_date` pre-fill the tag menu's
    date prompt when the tag is re-picked; a name-only remembered tag (from a
    ledger scan or legacy state) carries neither.
    """

    model_config = ConfigDict(frozen=True)

    tag: str
    from_date: date | None = None
    until_date: date | None = None


class TagState(BaseModel):
    """Persisted state. Loaded into ImportSession at start, written by CLI after run()."""

    model_config = ConfigDict(frozen=True)

    active: ActiveTag | None = None
    recent: tuple[RememberedTag, ...] = ()  # LRU of interacted tags (+ windows)

    def with_recent(self, tag: ActiveTag, cap: int = 10) -> TagState:
        """Return a new TagState with `tag` moved to the front of `recent`.

        Deduped by name (most-recent wins, so a re-used tag's window updates)
        and capped. Takes the full `ActiveTag` so the chosen window is kept.
        """
        return TagState(active=self.active, recent=remember(self.recent, tag, cap))

    def with_active(self, active: ActiveTag | None) -> TagState:
        return TagState(active=active, recent=self.recent)


def remember(
    recent: tuple[RememberedTag, ...], tag: ActiveTag, cap: int | None = None
) -> tuple[RememberedTag, ...]:
    """Prepend `tag` (with its window) to `recent`, deduped by name and capped.

    Shared by `TagState.with_recent` (persistence) and the pipeline's in-session
    known-tag list, so both grow the LRU identically.
    """
    entry = RememberedTag(
        tag=tag.tag, from_date=tag.from_date, until_date=tag.until_date
    )
    deduped = tuple(r for r in recent if r.tag != tag.tag)
    out = (entry, *deduped)
    return out[:cap] if cap is not None else out


def known_tags(
    recent: tuple[RememberedTag, ...], ledger_names: set[str]
) -> tuple[RememberedTag, ...]:
    """The tag-menu picker's source: interacted tags (with windows) first, then
    ledger tag names not already covered, as name-only entries."""
    seen = {r.tag for r in recent}
    extra = tuple(RememberedTag(tag=n) for n in sorted(ledger_names) if n not in seen)
    return (*recent, *extra)

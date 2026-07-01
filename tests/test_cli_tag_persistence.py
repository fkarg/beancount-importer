"""Tag-state persistence: `recent` now records interacted tags + their window."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer.cli import (
    _load_tag_state,
    _parse_remembered,
    _persist_tag_updates,
)
from beancount_importer.models import ImportResult, SourceTransaction
from beancount_importer.rules.tags import (
    ActiveTag,
    RememberedTag,
    TagState,
    TagStateDelta,
)


def _set_result(tag: str, until: date | None = None) -> ImportResult:
    state = ActiveTag(
        tag=tag, mode="duration" if until else "always", until_date=until
    )
    return ImportResult(
        source_txn=SourceTransaction(
            booking_date=date(2024, 3, 25), amount=Decimal("-1"), bank_key="paypal"
        ),
        action="new",
        tag_state_delta=TagStateDelta(op="set", new_state=state),
    )


def test_set_populates_recent_with_window(tmp_path: Path):
    path = tmp_path / "tag_state.json"
    _persist_tag_updates(
        [_set_result("usa-2024", until=date(2024, 4, 11))],
        TagState(),
        path,
        dry_run=False,
    )
    loaded = _load_tag_state(path)
    assert loaded.active is not None and loaded.active.tag == "usa-2024"
    # The window is remembered so the picker can pre-fill it next time.
    assert loaded.recent[0] == RememberedTag(
        tag="usa-2024", until_date=date(2024, 4, 11)
    )


def test_clear_keeps_recent(tmp_path: Path):
    path = tmp_path / "tag_state.json"
    start = TagState(
        active=ActiveTag(tag="usa-2024", mode="always"),
        recent=(RememberedTag(tag="usa-2024"),),
    )
    clear = ImportResult(
        source_txn=SourceTransaction(
            booking_date=date(2024, 3, 25), amount=Decimal("-1"), bank_key="paypal"
        ),
        action="new",
        tag_state_delta=TagStateDelta(op="clear"),
    )
    _persist_tag_updates([clear], start, path, dry_run=False)
    loaded = _load_tag_state(path)
    assert loaded.active is None
    assert [r.tag for r in loaded.recent] == ["usa-2024"]


def test_dry_run_does_not_write(tmp_path: Path):
    path = tmp_path / "tag_state.json"
    _persist_tag_updates([_set_result("x")], TagState(), path, dry_run=True)
    assert not path.exists()


def test_load_tolerates_legacy_string_recent(tmp_path: Path):
    path = tmp_path / "tag_state.json"
    path.write_text('{"active": null, "recent": ["old-a", "old-b"]}')
    loaded = _load_tag_state(path)
    assert loaded.recent == (RememberedTag(tag="old-a"), RememberedTag(tag="old-b"))


def test_parse_remembered_dict_with_dates():
    parsed = _parse_remembered({"tag": "t", "until_date": "2024-04-11"})
    assert parsed == RememberedTag(tag="t", until_date=date(2024, 4, 11))

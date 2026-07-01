from datetime import date

import pytest

from beancount_importer.rules.tags import (
    ActiveTag,
    RememberedTag,
    TagState,
    TagStateDelta,
    known_tags,
    remember,
)


def _at(name: str, **kw) -> ActiveTag:
    return ActiveTag(tag=name, mode=kw.pop("mode", "always"), **kw)


def _rt(name: str, **kw) -> RememberedTag:
    return RememberedTag(tag=name, **kw)


class TestActiveTagAppliesTo:
    def test_always_mode_always_applies(self):
        tag = ActiveTag(tag="trip-berlin", mode="always")
        assert tag.applies_to(date(2024, 1, 1))
        assert tag.applies_to(date(2030, 12, 31))

    def test_once_mode_always_applies(self):
        tag = ActiveTag(tag="lunch", mode="once")
        assert tag.applies_to(date(2024, 6, 15))

    def test_duration_within_range(self):
        tag = ActiveTag(
            tag="conf-2024",
            mode="duration",
            from_date=date(2024, 6, 10),
            until_date=date(2024, 6, 14),
        )
        assert tag.applies_to(date(2024, 6, 10))
        assert tag.applies_to(date(2024, 6, 12))
        assert tag.applies_to(date(2024, 6, 14))

    def test_duration_before_range(self):
        tag = ActiveTag(
            tag="conf-2024",
            mode="duration",
            from_date=date(2024, 6, 10),
            until_date=date(2024, 6, 14),
        )
        assert not tag.applies_to(date(2024, 6, 9))

    def test_duration_after_range(self):
        tag = ActiveTag(
            tag="conf-2024",
            mode="duration",
            from_date=date(2024, 6, 10),
            until_date=date(2024, 6, 14),
        )
        assert not tag.applies_to(date(2024, 6, 15))

    def test_duration_open_start(self):
        tag = ActiveTag(tag="x", mode="duration", until_date=date(2024, 6, 14))
        assert tag.applies_to(date(2020, 1, 1))
        assert not tag.applies_to(date(2024, 6, 15))

    def test_duration_open_end(self):
        tag = ActiveTag(tag="x", mode="duration", from_date=date(2024, 6, 10))
        assert not tag.applies_to(date(2024, 6, 9))
        assert tag.applies_to(date(2030, 1, 1))


class TestActiveTagIsExpired:
    def test_duration_expired(self):
        tag = ActiveTag(tag="x", mode="duration", until_date=date(2024, 6, 14))
        assert tag.is_expired(date(2024, 6, 15))

    def test_duration_not_expired_in_range(self):
        tag = ActiveTag(tag="x", mode="duration", until_date=date(2024, 6, 14))
        assert not tag.is_expired(date(2024, 6, 14))

    def test_always_never_expires(self):
        tag = ActiveTag(tag="x", mode="always")
        assert not tag.is_expired(date(2030, 1, 1))

    def test_once_never_expires_by_date(self):
        tag = ActiveTag(tag="x", mode="once")
        assert not tag.is_expired(date(2030, 1, 1))


class TestTagState:
    def test_default_empty(self):
        s = TagState()
        assert s.active is None
        assert s.recent == ()

    def test_with_recent_prepends(self):
        s = TagState().with_recent(_at("a")).with_recent(_at("b"))
        assert [r.tag for r in s.recent] == ["b", "a"]

    def test_with_recent_dedupes(self):
        s = TagState(recent=(_rt("a"), _rt("b"), _rt("c"))).with_recent(_at("b"))
        assert [r.tag for r in s.recent] == ["b", "a", "c"]

    def test_with_recent_keeps_the_window(self):
        s = TagState().with_recent(
            _at("usa-2024", mode="duration", until_date=date(2024, 4, 11))
        )
        assert s.recent[0] == RememberedTag(tag="usa-2024", until_date=date(2024, 4, 11))

    def test_with_recent_caps(self):
        s = TagState(recent=tuple(_rt(f"t{i}") for i in range(10)))
        s = s.with_recent(_at("new"), cap=10)
        assert len(s.recent) == 10
        assert s.recent[0].tag == "new"
        assert all(r.tag != "t9" for r in s.recent)  # oldest evicted

    def test_with_active_replaces(self):
        tag = ActiveTag(tag="x", mode="always")
        s = TagState().with_active(tag)
        assert s.active == tag

    def test_with_active_preserves_recent(self):
        s = TagState(recent=(_rt("a"), _rt("b"))).with_active(_at("x"))
        assert [r.tag for r in s.recent] == ["a", "b"]

    def test_frozen(self):
        s = TagState()
        with pytest.raises(Exception):
            s.recent = (_rt("x"),)  # type: ignore[misc]


class TestTagStateDelta:
    def test_set_op(self):
        d = TagStateDelta(op="set", new_state=ActiveTag(tag="x", mode="once"))
        assert d.op == "set"
        assert d.new_state is not None
        assert d.new_state.tag == "x"

    def test_clear_op(self):
        d = TagStateDelta(op="clear")
        assert d.op == "clear"
        assert d.new_state is None

    def test_noop(self):
        d = TagStateDelta(op="noop")
        assert d.op == "noop"


class TestRemember:
    def test_prepends_and_captures_window(self):
        recent = (_rt("a"), _rt("b"))
        out = remember(
            recent, _at("b", mode="duration", until_date=date(2024, 4, 11))
        )
        # Moved to front, deduped, and its window updated.
        assert [r.tag for r in out] == ["b", "a"]
        assert out[0].until_date == date(2024, 4, 11)

    def test_caps_when_requested(self):
        recent = tuple(_rt(f"t{i}") for i in range(5))
        out = remember(recent, _at("new"), cap=3)
        assert [r.tag for r in out] == ["new", "t0", "t1"]

    def test_no_cap_keeps_all(self):
        out = remember((_rt("a"),), _at("x"))
        assert [r.tag for r in out] == ["x", "a"]


class TestKnownTags:
    def test_recent_first_then_ledger_sorted(self):
        recent = (_rt("usa-2024", until_date=date(2024, 4, 11)), _rt("italy"))
        ledger = {"italy", "amazon", "rewe"}  # italy duplicates a recent tag
        out = known_tags(recent, ledger)
        assert [r.tag for r in out] == ["usa-2024", "italy", "amazon", "rewe"]
        # recent keeps its window; ledger-only entries are name-only.
        assert out[0].until_date == date(2024, 4, 11)
        assert out[2].until_date is None

    def test_empty_ledger_is_just_recent(self):
        recent = (_rt("a"),)
        assert known_tags(recent, set()) == recent

from datetime import date

import pytest

from beancount_importer.rules.tags import ActiveTag, TagState, TagStateDelta


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
        s = TagState().with_recent("a").with_recent("b")
        assert s.recent == ("b", "a")

    def test_with_recent_dedupes(self):
        s = TagState(recent=("a", "b", "c")).with_recent("b")
        assert s.recent == ("b", "a", "c")

    def test_with_recent_caps(self):
        s = TagState(recent=tuple(f"t{i}" for i in range(10)))
        s = s.with_recent("new", cap=10)
        assert len(s.recent) == 10
        assert s.recent[0] == "new"
        assert "t9" not in s.recent  # oldest evicted

    def test_with_active_replaces(self):
        tag = ActiveTag(tag="x", mode="always")
        s = TagState().with_active(tag)
        assert s.active == tag

    def test_with_active_preserves_recent(self):
        s = TagState(recent=("a", "b")).with_active(ActiveTag(tag="x", mode="always"))
        assert s.recent == ("a", "b")

    def test_frozen(self):
        s = TagState()
        with pytest.raises(Exception):
            s.recent = ("x",)  # type: ignore[misc]


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

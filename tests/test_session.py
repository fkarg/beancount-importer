import pytest

from beancount_importer.config import Config
from beancount_importer.session import ImportOptions, ImportSession
from beancount_importer.rules.tags import ActiveTag, TagState


class TestImportOptions:
    def test_defaults(self):
        o = ImportOptions()
        assert o.interactive is True
        assert o.auto_update is False
        assert o.auto_threshold is None
        assert o.dry_run is False
        assert o.bank_filter is None

    def test_frozen(self):
        o = ImportOptions()
        with pytest.raises(Exception):
            o.dry_run = True  # type: ignore[misc]


class TestImportSession:
    def test_minimal_construction(self):
        s = ImportSession(config=Config())
        assert s.rules == ()
        assert s.tag_state == TagState()

    def test_with_active_tag(self):
        ts = TagState(active=ActiveTag(tag="x", mode="always"))
        s = ImportSession(config=Config(), tag_state=ts)
        assert s.tag_state.active is not None
        assert s.tag_state.active.tag == "x"

    def test_frozen(self):
        s = ImportSession(config=Config())
        with pytest.raises(Exception):
            s.config = Config()  # type: ignore[misc]

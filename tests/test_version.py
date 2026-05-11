"""Version and source-hash helpers exposed at package root."""

from __future__ import annotations

import re

import beancount_importer
from beancount_importer import __version__, source_hash


def test_version_is_semver_or_unknown() -> None:
    # Either a real metadata version or the documented unknown sentinel.
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", __version__) or __version__ == "0.0.0+unknown"


def test_source_hash_shape_and_cache() -> None:
    digest = source_hash()
    # 12 hex chars by design (first 12 of SHA-256).
    assert re.fullmatch(r"[0-9a-f]{12}", digest)
    # Cached: second call returns the same string instance.
    assert source_hash() is digest


def test_source_hash_cache_recomputes_when_cleared() -> None:
    # Clearing the cache forces a recompute; the value is stable for a
    # given source tree, so the result must equal the original.
    original = source_hash()
    beancount_importer._CACHED_SOURCE_HASH = None
    assert source_hash() == original

"""Suite-wide pytest fixtures.

The claim→evidence cache (Story 2.8) is ENABLED by default in production. The test suite must
NOT use it: existing engine/provider/search tests call `retrieve_evidence` indirectly and rely
on the live chain running every time, and a real default cache would write a stray
`proov_cache.db` and leak state across tests. So this autouse fixture disables the default
cache for the WHOLE suite and resets the memoised instance before and after each test.

Targeted cache tests opt back in by INJECTING `cache=SqliteEvidenceCache(...)` directly into
`retrieve_evidence` (or by constructing the cache classes under test) — bypassing the disabled
default. Mirrors the autouse env-isolation precedent in `tests/test_search.py`.
"""

from __future__ import annotations

import pytest

from proov.cache import reset_evidence_cache


@pytest.fixture(autouse=True)
def _disable_evidence_cache(monkeypatch):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", "0")
    reset_evidence_cache()
    yield
    reset_evidence_cache()

"""Suite-wide pytest fixtures.

The claim→evidence cache (Story 2.8) and the order/metrics ledger (Story 3.2) are both ENABLED
by default in production. The test suite must NOT use either: existing engine/provider/search
tests call `retrieve_evidence` indirectly and rely on the live chain running every time, the
provider records to the ledger at every terminal order, and a real default cache/ledger would
write a stray `proov_cache.db` / `proov_ledger.db` and leak state across tests. So these autouse
fixtures disable the defaults for the WHOLE suite and reset the memoised instances before and
after each test.

Targeted cache/ledger tests opt back in by INJECTING the backend directly (e.g.
`SqliteEvidenceCache(...)` / `SqliteOrderLedger(":memory:")`) or by monkeypatching the env back
on — bypassing the disabled default. Mirrors the autouse env-isolation precedent in
`tests/test_search.py`.
"""

from __future__ import annotations

import pytest

from proov.cache import reset_evidence_cache
from proov.ledger import reset_order_ledger


@pytest.fixture(autouse=True)
def _disable_evidence_cache(monkeypatch):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", "0")
    reset_evidence_cache()
    yield
    reset_evidence_cache()


@pytest.fixture(autouse=True)
def _disable_order_ledger(monkeypatch):
    monkeypatch.setenv("PROOV_LEDGER_ENABLED", "0")
    reset_order_ledger()
    yield
    reset_order_ledger()

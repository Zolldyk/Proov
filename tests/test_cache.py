"""Tests for the claim→evidence cache (`proov/cache.py`) — OFFLINE ONLY.

Real `sqlite3` against `:memory:` / `tmp_path`, an injected clock for TTL, no network, no
wall-clock waits, no API spend. The autouse fixture in `conftest.py` keeps the default cache
disabled suite-wide; these tests construct the cache classes directly.
"""

from __future__ import annotations

import pytest

from proov.cache import (
    EvidenceCache,
    NullCache,
    SqliteEvidenceCache,
    _DEFAULT_TTL_SECONDS,
    _resolve_ttl_seconds,
    evidence_cache_key,
    get_evidence_cache,
    reset_evidence_cache,
)
from proov.types import Evidence

# --------------------------------------------------------------------------- helpers


def _ev(source: str = "https://a", score=None) -> Evidence:
    return Evidence(source=source, title="t", snippet="s", score=score)


class _Clock:
    """A mutable fake wall clock; `advance` moves it forward deterministically."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, by: float) -> None:
        self.now += by


# --------------------------------------------------------------------------- key helper


def test_key_is_deterministic():
    assert evidence_cache_key("the earth is round", "quick", 3) == evidence_cache_key(
        "the earth is round", "quick", 3
    )


def test_key_is_case_and_whitespace_insensitive():
    assert evidence_cache_key("The  Earth   IS Round ", "quick", 3) == evidence_cache_key(
        "the earth is round", "quick", 3
    )


def test_key_differs_by_tier():
    assert evidence_cache_key("q", "quick", 3) != evidence_cache_key("q", "deep", 3)


def test_key_differs_by_k():
    assert evidence_cache_key("q", "quick", 3) != evidence_cache_key("q", "quick", 6)


# --------------------------------------------------------------------------- round-trip


async def test_roundtrip_memory_score_none():
    cache = SqliteEvidenceCache(":memory:")
    evidence = [_ev("https://a", None), _ev("https://b", None)]
    await cache.put("k", evidence)
    assert await cache.get("k") == evidence


async def test_roundtrip_float_score():
    cache = SqliteEvidenceCache(":memory:")
    evidence = [_ev("https://a", 0.87)]
    await cache.put("k", evidence)
    got = await cache.get("k")
    assert got == evidence
    assert got[0].score == 0.87


async def test_roundtrip_file_path(tmp_path):
    db = tmp_path / "c.db"
    cache = SqliteEvidenceCache(str(db))
    evidence = [_ev("https://a", 0.5)]
    await cache.put("k", evidence)
    assert await cache.get("k") == evidence
    assert db.exists()


async def test_unknown_key_is_miss():
    cache = SqliteEvidenceCache(":memory:")
    assert await cache.get("nope") is None


async def test_put_replaces_existing_key():
    cache = SqliteEvidenceCache(":memory:")
    await cache.put("k", [_ev("https://old")])
    await cache.put("k", [_ev("https://new")])
    got = await cache.get("k")
    assert [e.source for e in got] == ["https://new"]


# --------------------------------------------------------------------------- TTL


async def test_ttl_expiry_is_a_miss_and_prunes_row():
    clock = _Clock()
    cache = SqliteEvidenceCache(":memory:", ttl_seconds=100.0, clock=clock)
    await cache.put("k", [_ev()])
    clock.advance(50)
    assert await cache.get("k") is not None  # still fresh
    clock.advance(60)  # now 110s old > 100s TTL
    assert await cache.get("k") is None  # expired → miss
    # Stale row was pruned: a direct row count is 0.
    rows = cache._conn.execute("SELECT COUNT(*) FROM evidence_cache").fetchone()[0]
    assert rows == 0


async def test_ttl_boundary_not_yet_expired():
    clock = _Clock()
    cache = SqliteEvidenceCache(":memory:", ttl_seconds=100.0, clock=clock)
    await cache.put("k", [_ev()])
    clock.advance(100)  # exactly at TTL — `>` means not yet expired
    assert await cache.get("k") is not None


# --------------------------------------------------------------------------- best-effort degrade


async def test_get_on_closed_conn_degrades_to_miss():
    cache = SqliteEvidenceCache(":memory:")
    await cache.put("k", [_ev()])
    cache.close()
    assert await cache.get("k") is None  # no raise


async def test_put_on_closed_conn_is_noop():
    cache = SqliteEvidenceCache(":memory:")
    cache.close()
    await cache.put("k", [_ev()])  # no raise


async def test_get_on_corrupt_blob_degrades_to_miss():
    cache = SqliteEvidenceCache(":memory:")
    # Write a malformed blob directly so `_from_json` raises inside the best-effort wrapper.
    with cache._lock:
        cache._conn.execute(
            "INSERT INTO evidence_cache (key, evidence_json, created_at) VALUES (?,?,?)",
            ("k", "{not json", cache._clock()),
        )
        cache._conn.commit()
    assert await cache.get("k") is None  # degrades, no raise


# --------------------------------------------------------------------------- NullCache


async def test_null_cache_get_is_none_put_is_noop():
    cache = NullCache()
    assert await cache.get("k") is None
    await cache.put("k", [_ev()])
    assert await cache.get("k") is None


# --------------------------------------------------------------------------- factory


def test_factory_disabled_returns_nullcache(monkeypatch):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", "0")
    reset_evidence_cache()
    assert isinstance(get_evidence_cache(), NullCache)
    reset_evidence_cache()


@pytest.mark.parametrize("falsey", ["0", "false", "No", "OFF"])
def test_factory_falsey_values_disable(monkeypatch, falsey):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", falsey)
    reset_evidence_cache()
    assert isinstance(get_evidence_cache(), NullCache)
    reset_evidence_cache()


def test_factory_enabled_builds_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", "1")
    monkeypatch.setenv("PROOV_CACHE_PATH", str(tmp_path / "c.db"))
    reset_evidence_cache()
    cache = get_evidence_cache()
    assert isinstance(cache, SqliteEvidenceCache)
    # Memoised: a repeat call returns the SAME instance.
    assert get_evidence_cache() is cache
    reset_evidence_cache()


def test_reset_clears_memoised_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", "1")
    monkeypatch.setenv("PROOV_CACHE_PATH", str(tmp_path / "c.db"))
    reset_evidence_cache()
    first = get_evidence_cache()
    reset_evidence_cache()
    assert get_evidence_cache() is not first
    reset_evidence_cache()


def test_factory_unopenable_path_degrades_to_nullcache(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOV_CACHE_ENABLED", "1")
    # A path under a non-existent directory cannot be opened → degrade, not raise.
    monkeypatch.setenv("PROOV_CACHE_PATH", str(tmp_path / "no_such_dir" / "c.db"))
    reset_evidence_cache()
    assert isinstance(get_evidence_cache(), NullCache)
    reset_evidence_cache()


# --------------------------------------------------------------------------- TTL resolver


def test_resolve_ttl_defaults_and_rejects_garbage():
    assert _resolve_ttl_seconds(None) == _DEFAULT_TTL_SECONDS
    assert _resolve_ttl_seconds("not-a-number") == _DEFAULT_TTL_SECONDS
    assert _resolve_ttl_seconds("0") == _DEFAULT_TTL_SECONDS
    assert _resolve_ttl_seconds("-5") == _DEFAULT_TTL_SECONDS
    assert _resolve_ttl_seconds("inf") == _DEFAULT_TTL_SECONDS
    assert _resolve_ttl_seconds("nan") == _DEFAULT_TTL_SECONDS
    assert _resolve_ttl_seconds("3600") == 3600.0


# --------------------------------------------------------------------------- conformance


def test_classes_conform_to_protocol():
    assert isinstance(SqliteEvidenceCache(":memory:"), EvidenceCache)
    assert isinstance(NullCache(), EvidenceCache)

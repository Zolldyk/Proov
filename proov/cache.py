"""Claim→evidence cache `[E]` — a TTL'd, SQLite-backed, best-effort retrieval cache (FR11).

Architecture §2/§4 name `[E] Cache` as its own component: a SQLite `claim→evidence` cache
that cuts cost and latency and is *"the key enabler of the $0 model at volume"*. Story 2.2
deliberately left `retrieve_evidence` cache-friendly but cache-free ("leave a clean seam; do
not add SQLite here"); this module fills that seam.

It mirrors the provider discipline of `proov/search.py`: everything hangs off a structural
`EvidenceCache` Protocol, so the SQLite backend is swappable (architecture §7) and a cache
HIT returns the same `list[Evidence]` the live path would have produced — the cache changes
timing/cost, NEVER the data shape or the verdict.

Design calls (see the story Dev Notes for the full rationale):

- **Key = `sha256(tier, k, normalised_claim)`**, NOT bare `hash(normalized_claim)`. Quick and
  Deep produce structurally different result sets for the same claim (Quick: first non-empty
  provider, `k=3`; Deep: multi-source merge, `k=6`), so keying on `(normalised_query, tier, k)`
  stops a Quick entry from poisoning a Deep read (and vice-versa). A deliberate, documented
  strengthening of the architecture text.
- **Best-effort.** Every SQLite/JSON failure degrades to a miss (`get`) / no-op (`put`) /
  `NullCache` (factory) — `retrieve_evidence` must still NEVER raise out of a paid order
  (NFR3). The cache can only make things faster/cheaper; it can never fail an order.
- **TTL via a stored wall-clock timestamp** (NOT the engine's monotonic SLA clock — the cache
  persists across process restarts where a monotonic clock resets). Expiry is lazy, on read.
- **One lock-guarded connection, off-loaded via `asyncio.to_thread`**, so the always-on
  WebSocket event loop + heartbeat (architecture §1/§3) is never blocked on disk I/O, and a
  shared `:memory:` DB works across operations (the offline tests rely on it).

Stdlib only — `sqlite3`/`json`/`hashlib`/`time`/`asyncio`/`threading`/`re`/`os`/`logging`. NO
`croo` import (this is `[E]` engine-side code; `proov/provider.py` stays the only CROO-coupled
module), NO `httpx`, NO new dependency. This module owns ONLY the evidence table — the
order/metrics ledger `[E]` is Story 3.2 (it may later add a separate table to the same `.db`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from .types import Evidence, Tier

logger = logging.getLogger("proov.cache")

# 24 h — long enough that the demo + re-verifications hit cache, short enough that evidence
# does not go stale for days. Env-overridable via PROOV_CACHE_TTL_SECONDS.
_DEFAULT_TTL_SECONDS = 86400.0

# Story 3.3 size-cap: the lazy TTL-on-read prune never evicts a write-once-never-read key, so at
# volume the table could grow without bound (disk fill / slowdown). A best-effort size-cap prune
# on `put` bounds it. Env-overridable via PROOV_CACHE_MAX_ROWS; ≤0 disables the cap.
_DEFAULT_MAX_ROWS = 10000

# A single dedicated table. The order/metrics ledger (Story 3.2) is a SEPARATE table/concern —
# this name leaves the door open to share the same `.db` file later without coupling now.
_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS evidence_cache ("
    "key TEXT PRIMARY KEY, evidence_json TEXT NOT NULL, created_at REAL NOT NULL)"
)

_WHITESPACE_RE = re.compile(r"\s+")


def _resolve_ttl_seconds(raw: str | None) -> float:
    """Parse `PROOV_CACHE_TTL_SECONDS`, tolerating garbage by falling back to the default.

    Hardened identically to `proov.search._resolve_timeout`: a None / non-numeric / non-finite
    (inf/nan) / ≤0 value falls back to the 24 h default — a garbage TTL must never disable
    expiry (an infinite TTL would pin stale evidence forever) or expire everything instantly.
    """
    if raw is None:
        return _DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS
    # `math.isfinite` without importing math: NaN != itself; inf is unbounded.
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return _DEFAULT_TTL_SECONDS
    return value


def _resolve_max_rows(raw: str | None) -> int:
    """Parse `PROOV_CACHE_MAX_ROWS`, tolerating garbage with the default (10000).

    The size-cap (Story 3.3): a row count above this triggers a best-effort prune of the oldest
    rows on `put`. A None / non-int value → the 10000 default; a ≤0 value → `0` (the disabled
    sentinel — no cap, matching the env contract "≤0 disables"). Mirrors the hardened resolver
    idiom, but ≤0 is a meaningful "disable" here rather than a garbage→default.
    """
    if raw is None:
        return _DEFAULT_MAX_ROWS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROWS
    # ≤0 explicitly DISABLES the cap (sentinel 0); a positive value is the cap.
    return value if value > 0 else 0


def _normalise_query(query: str) -> str:
    """Case/whitespace-insensitive normalisation so trivially-varying restatements collide.

    Lower-cases, collapses internal whitespace to single spaces, and strips — a deterministic
    normalisation so `"The Earth is round"` and `"the  earth   is round"` hit the same key.
    """
    return _WHITESPACE_RE.sub(" ", query.strip().lower())


def evidence_cache_key(query: str, tier: Tier, k: int) -> str:
    """`sha256` hex of the `(tier, k, normalised_query)` composite — the anti-poison key.

    Including `tier` and `k` (not just the normalised claim) guarantees a cached Quick result
    (single source, `k=3`) is NEVER served to a Deep request (which paid for the 6-item
    multi-source merge), and a result capped at one `k` is never served for a different `k`.
    The `\\x00` separators keep the fields unambiguous (no value can contain a NUL).
    """
    composite = f"{tier}\x00{k}\x00{_normalise_query(query)}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


def _to_json(evidence: list[Evidence]) -> str:
    """Serialise `list[Evidence]` to plain JSON (NOT canonical — this value is never hashed).

    The cache value is local state only; `score` (a float | None) deliberately never enters a
    hashed/anchored structure, so the receipt's float-canonicalisation contract does not apply.
    """
    return json.dumps(
        [
            {"source": e.source, "title": e.title, "snippet": e.snippet, "score": e.score}
            for e in evidence
        ]
    )


def _from_json(blob: str) -> list[Evidence]:
    """Deserialise back to `list[Evidence]`. A malformed/extra-key blob raises (caught upstream).

    `Evidence(**d)` reconstructs each entry field-for-field (`score=None` ↔ JSON `null`, a float
    `score` ↔ JSON number). An unexpected shape raises `TypeError`/`json.JSONDecodeError`, which
    the best-effort `get` wrapper catches and degrades to a miss.
    """
    return [Evidence(**d) for d in json.loads(blob)]


@runtime_checkable
class EvidenceCache(Protocol):
    """Structural interface every evidence cache satisfies (architecture §7 discipline).

    Declares ONLY `get`/`put`. Because it is `@runtime_checkable`, conformance is a cheap
    `isinstance` test and a future backend (Redis, etc.) need only implement these two methods.
    """

    async def get(self, key: str) -> list[Evidence] | None: ...

    async def put(self, key: str, evidence: list[Evidence]) -> None: ...


class NullCache:
    """No-op `EvidenceCache` — the disabled / unavailable sentinel.

    Used when caching is turned off (`PROOV_CACHE_ENABLED=0`), when the SQLite file cannot be
    opened (degrade), and by the test suite's autouse fixture so existing tests keep their exact
    behaviour. `get` always misses, `put` always no-ops.
    """

    async def get(self, key: str) -> list[Evidence] | None:
        return None

    async def put(self, key: str, evidence: list[Evidence]) -> None:
        return None


class SqliteEvidenceCache:
    """SQLite-backed `EvidenceCache` — one lock-guarded connection, `to_thread`-offloaded, TTL'd.

    One `sqlite3` connection (`check_same_thread=False`) guarded by a `threading.Lock` serialises
    every access (correct under the Story 3.3 worker-pool too); a single shared connection is
    required so a `:memory:` DB shares state across ops (the offline tests rely on it). Each
    blocking SQL op runs inside `await asyncio.to_thread(...)` so the event-loop heartbeat is
    never blocked on disk I/O. Works identically for `:memory:` and a file path.

    Best-effort throughout: any `sqlite3.Error` / JSON error / unexpected `Exception` in `get`
    degrades to a miss (`None`) and in `put` to a no-op — `retrieve_evidence` still never raises
    out (NFR3). `asyncio.CancelledError` is a `BaseException` and does not surface inside the
    sync bodies run via `to_thread`, so cancellation still propagates.
    """

    def __init__(
        self,
        path: str,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_rows: int = _DEFAULT_MAX_ROWS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        # Story 3.3 size-cap: prune oldest rows on `put` once the count exceeds this. ≤0 disables.
        self._max_rows = max_rows
        # Wall clock (NOT a monotonic clock): the cache persists across process restarts, where
        # a monotonic clock resets. Injectable so TTL expiry is deterministically testable.
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # Pragmas are best-effort tuning — some filesystems (e.g. network/FUSE mounts) reject
        # WAL. A pragma failure must degrade only that pragma, NOT disable caching entirely
        # (without this guard the exception bubbles to the factory, which would drop the whole
        # process to NullCache). The table itself is load-bearing, so its creation stays outside.
        try:
            # Wait (rather than immediately erroring) when another op holds the write lock.
            self._conn.execute("PRAGMA busy_timeout=5000")
            # WAL improves concurrent read/write robustness on a real file; it is meaningless
            # (and rejected) for an in-memory DB, so guard it.
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            logger.warning("evidence cache: pragma setup failed; continuing without it", exc_info=True)
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def _get_sync(self, key: str) -> list[Evidence] | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT evidence_json, created_at FROM evidence_cache WHERE key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                blob, created_at = row
                age = self._clock() - created_at
                # A negative age means the wall clock moved backward (e.g. an NTP correction)
                # after the row was written; treat such a future-dated entry as expired too, so
                # it can never become un-expirable until the clock catches up.
                if age < 0 or age > self._ttl_seconds:
                    # Expired → miss. Prune the stale row best-effort (hygiene); a DELETE
                    # failure is swallowed — it is still a miss either way.
                    try:
                        self._conn.execute(
                            "DELETE FROM evidence_cache WHERE key=?", (key,)
                        )
                        self._conn.commit()
                    except sqlite3.Error:
                        pass
                    return None
                return _from_json(blob)
        except Exception as exc:  # noqa: BLE001
            # Best-effort: a locked/corrupt/disk-full DB or a malformed blob degrades to a miss
            # (→ live search runs). `CancelledError` is a BaseException — not caught here.
            logger.warning("Evidence cache get failed, treating as miss: %r", exc)
            return None

    async def get(self, key: str) -> list[Evidence] | None:
        return await asyncio.to_thread(self._get_sync, key)

    def _put_sync(self, key: str, evidence: list[Evidence]) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO evidence_cache "
                    "(key, evidence_json, created_at) VALUES (?, ?, ?)",
                    (key, _to_json(evidence), self._clock()),
                )
                self._conn.commit()
                # Story 3.3 size-cap prune (best-effort, under the same lock, AFTER the row is
                # committed). Its OWN try/except swallows any `sqlite3.Error` so a prune failure
                # can never fail a `put` — the row is already stored.
                self._prune_to_cap()
        except Exception as exc:  # noqa: BLE001
            # Best-effort no-op: the order still delivers from the live `result`.
            logger.warning("Evidence cache put failed, skipping store: %r", exc)

    def _prune_to_cap(self) -> None:
        """Best-effort: prune the oldest rows down to `max_rows` (Story 3.3). Called under lock.

        Lazy TTL-on-read never evicts a write-once-never-read key, so without this the table can
        grow without bound at volume. After a `put`, if the row count exceeds `max_rows`, delete
        the oldest rows (by `created_at`) down to the cap. ≤0 `max_rows` disables it. A prune
        failure is swallowed (`sqlite3.Error`) — it must NEVER fail a `put` (the row is stored).
        """
        if self._max_rows <= 0:
            return
        try:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM evidence_cache"
            ).fetchone()[0]
            if count > self._max_rows:
                self._conn.execute(
                    "DELETE FROM evidence_cache WHERE key IN ("
                    "SELECT key FROM evidence_cache ORDER BY created_at ASC, rowid ASC LIMIT ?)",
                    (count - self._max_rows,),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Evidence cache size-cap prune skipped: %r", exc)

    async def put(self, key: str, evidence: list[Evidence]) -> None:
        await asyncio.to_thread(self._put_sync, key, evidence)

    def close(self) -> None:
        """Best-effort close of the underlying connection (used by `reset_evidence_cache`)."""
        try:
            with self._lock:
                self._conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence cache close failed: %r", exc)


# Memoised default cache. `retrieve_evidence` is called once per claim, so a fresh
# `SqliteEvidenceCache` per claim would churn connections and reset a `:memory:` DB — build it
# once and reuse it. The test suite resets this between tests (see `tests/conftest.py`).
_default_cache: EvidenceCache | None = None

# Story 3.3: the worker-pool means real concurrent `get_evidence_cache()` callers (two order
# tasks' `to_thread` SQL can race to build two connections, leaking one). A module-level lock +
# double-checked locking closes the factory race. The factories are sync, so a `threading.Lock`
# (not an asyncio lock) is the right primitive.
_factory_lock = threading.Lock()

_FALSEY = {"0", "false", "no", "off"}


def get_evidence_cache() -> EvidenceCache:
    """Return the memoised default `EvidenceCache` (production enabled; degrade to `NullCache`).

    Reads `PROOV_CACHE_ENABLED` (default ENABLED; `0`/`false`/`no`/`off`, case-insensitive →
    `NullCache`), `PROOV_CACHE_PATH` (default `"proov_cache.db"`, gitignored via `*.db`),
    `PROOV_CACHE_TTL_SECONDS` (default 86400) and `PROOV_CACHE_MAX_ROWS` (default 10000; ≤0
    disables the size-cap). A failure to open the DB logs a warning and memoises a `NullCache`
    (degrade — caching must never crash the engine).

    Story 3.3: built under `_factory_lock` with double-checked locking so concurrent callers
    (the worker-pool's order tasks) cannot build two connections and leak one.
    """
    global _default_cache
    if _default_cache is not None:
        return _default_cache

    with _factory_lock:
        # Double-checked: another thread may have built the singleton while we waited for the lock.
        if _default_cache is not None:
            return _default_cache

        enabled = os.environ.get("PROOV_CACHE_ENABLED", "1").strip().lower()
        if enabled in _FALSEY:
            _default_cache = NullCache()
            return _default_cache

        try:
            _default_cache = SqliteEvidenceCache(
                os.environ.get("PROOV_CACHE_PATH", "proov_cache.db"),
                ttl_seconds=_resolve_ttl_seconds(os.environ.get("PROOV_CACHE_TTL_SECONDS")),
                max_rows=_resolve_max_rows(os.environ.get("PROOV_CACHE_MAX_ROWS")),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence cache unavailable, caching disabled: %r", exc)
            _default_cache = NullCache()
        return _default_cache


def reset_evidence_cache() -> None:
    """Close + clear the memoised default cache (test seam — `tests/conftest.py` autouse)."""
    global _default_cache
    if _default_cache is not None:
        close = getattr(_default_cache, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Evidence cache reset close failed: %r", exc)
    _default_cache = None

"""Order/metrics ledger `[E]` — a best-effort, SQLite-backed order log mirroring `list_orders`.

Architecture §2/§3 name `[E]` as carrying *"a local order/metrics ledger mirroring
`list_orders` for the success/counter-metric dashboard"*, and `proov/cache.py:30-33,56-61`
explicitly defers it here ("the order/metrics ledger `[E]` is Story 3.2 … a SEPARATE table/
concern"). This module fills that seam. It stores the facts a live `list_orders` read does NOT
know — the **tier** and the **per-order cost** — plus a snapshot of counterparty/wallet/price/
status so the **offline** dashboard works with no network. `scripts/dashboard.py` reconciles
this ledger with `list_orders` (live status wins; the ledger supplies tier/cost).

Its structural template is `proov/cache.py` `SqliteEvidenceCache` (copy the discipline, not the
schema):

- **One lock-guarded `sqlite3` connection**, every blocking op off-loaded via
  `asyncio.to_thread`, so the always-on WebSocket heartbeat (architecture §1/§3) is never
  blocked on disk I/O and a shared `:memory:` DB works across operations (the offline tests
  rely on it).
- **Best-effort throughout.** Every `sqlite3.Error` / unexpected `Exception` degrades `record`
  to a no-op and `all_orders` to `[]` — the ledger can make Proov *observable*, it can NEVER
  fail or slow a paid order (NFR3). `asyncio.CancelledError` is a `BaseException` and is not
  caught, so cancellation still propagates.
- **Idempotent per `order_id`** (`INSERT OR REPLACE` upsert), matching the provider's delivery
  idempotency: a re-recorded order updates in place.

Stdlib only — `sqlite3`/`asyncio`/`threading`/`os`/`time`/`logging`. NO `croo` import (this is
`[E]` engine-side state; `provider.py` stays the only
CROO-coupled module); the `OrderRecord` value type is imported from the pure `proov/metrics.py`.
NO new dependency. The ledger `*.db` is gitignored via the existing `*.db` rule.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from .metrics import OrderRecord

logger = logging.getLogger("proov.ledger")

# A single dedicated `orders` table, keyed by `order_id` (the upsert key). `recorded_at` is
# local hygiene (when Proov wrote the row) — NOT a metric input. This is the SEPARATE table the
# `cache.py:56-61` note anticipated; it lives in its own `.db` by default for a cleaner
# lifecycle (the cache is TTL'd/evictable, the ledger is an append/upsert audit trail).
_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS orders ("
    "order_id TEXT PRIMARY KEY, status TEXT, tier TEXT, requester_agent_id TEXT, "
    "requester_wallet_address TEXT, provider_agent_id TEXT, price_usd REAL, cost_usd REAL, "
    "recorded_at REAL)"
)


@runtime_checkable
class OrderLedger(Protocol):
    """Structural interface every order ledger satisfies (architecture §7 discipline).

    Declares ONLY `record`/`all_orders`. Because it is `@runtime_checkable`, conformance is a
    cheap `isinstance` test and a future backend need only implement these two methods.
    """

    async def record(self, record: OrderRecord) -> None: ...

    async def all_orders(self) -> list[OrderRecord]: ...


class NullLedger:
    """No-op `OrderLedger` — the disabled / unavailable / degrade sentinel and the test default.

    Used when the ledger is turned off (`PROOV_LEDGER_ENABLED=0`), when the SQLite file cannot
    be opened (degrade), and by the suite's autouse fixture so existing tests keep their exact
    behaviour. `record` no-ops, `all_orders` always returns `[]`.
    """

    async def record(self, record: OrderRecord) -> None:
        return None

    async def all_orders(self) -> list[OrderRecord]:
        return []


class SqliteOrderLedger:
    """SQLite-backed `OrderLedger` — one lock-guarded connection, `to_thread`-offloaded, upsert.

    One `sqlite3` connection (`check_same_thread=False`) guarded by a `threading.Lock` serialises
    every access (correct under the future Story 3.3 worker-pool too); a single shared connection
    is required so a `:memory:` DB shares state across ops (the offline tests rely on it). Each
    blocking SQL op runs inside `await asyncio.to_thread(...)` so the event-loop heartbeat is
    never blocked on disk I/O. Works identically for `:memory:` and a file path.

    Best-effort throughout: any `sqlite3.Error` / unexpected `Exception` in `record` degrades to
    a no-op and in `all_orders` to `[]` — a paid order still never slows or fails (NFR3).
    `asyncio.CancelledError` is a `BaseException` and does not surface inside the sync bodies run
    via `to_thread`, so cancellation still propagates.
    """

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time) -> None:
        # Wall clock (NOT monotonic): the ledger persists across process restarts where a
        # monotonic clock resets. Injectable so `recorded_at` is deterministic in tests.
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # Pragmas are best-effort tuning — some filesystems (network/FUSE mounts) reject WAL. A
        # pragma failure must degrade only that pragma, NOT disable the ledger entirely (without
        # this guard the exception bubbles to the factory, which would drop to NullLedger). The
        # table itself is load-bearing, so its creation stays outside the guard. (cache.py:190-200)
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            logger.warning("order ledger: pragma setup failed; continuing without it", exc_info=True)
        # The table is load-bearing: if it cannot be created (corrupt/disk-full DB) we let the
        # exception bubble to `get_order_ledger` (which degrades to NullLedger) — but close the
        # already-open connection first so the degrade path does not leak a file handle.
        try:
            self._conn.execute(_CREATE_TABLE)
            self._conn.commit()
        except Exception:
            self._conn.close()
            raise

    def _record_sync(self, record: OrderRecord) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO orders "
                    "(order_id, status, tier, requester_agent_id, requester_wallet_address, "
                    "provider_agent_id, price_usd, cost_usd, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.order_id,
                        record.status,
                        record.tier,
                        record.requester_agent_id,
                        record.requester_wallet_address,
                        record.provider_agent_id,
                        record.price_usd,
                        record.cost_usd,
                        self._clock(),
                    ),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            # Best-effort no-op: a locked/corrupt/disk-full DB must never fail a paid order
            # (NFR3). `CancelledError` is a BaseException — not caught here.
            logger.warning("Order ledger record failed, skipping store: %r", exc)

    async def record(self, record: OrderRecord) -> None:
        await asyncio.to_thread(self._record_sync, record)

    def _all_sync(self) -> list[OrderRecord]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT order_id, status, tier, requester_agent_id, "
                    "requester_wallet_address, provider_agent_id, price_usd, cost_usd FROM orders"
                ).fetchall()
            return [
                OrderRecord(
                    order_id=row[0],
                    status=row[1],
                    tier=row[2],
                    requester_agent_id=row[3],
                    requester_wallet_address=row[4],
                    provider_agent_id=row[5],
                    price_usd=row[6],
                    cost_usd=row[7],
                )
                for row in rows
            ]
        except Exception as exc:  # noqa: BLE001
            # Best-effort: a read failure degrades to an empty list (the dashboard shows zeros /
            # n/a rather than crashing). `CancelledError` is a BaseException — not caught.
            logger.warning("Order ledger read failed, returning no orders: %r", exc)
            return []

    async def all_orders(self) -> list[OrderRecord]:
        return await asyncio.to_thread(self._all_sync)

    def close(self) -> None:
        """Best-effort close of the underlying connection (used by `reset_order_ledger`)."""
        try:
            with self._lock:
                self._conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Order ledger close failed: %r", exc)


# Memoised default ledger. The provider records once per terminal order, so a fresh
# `SqliteOrderLedger` per order would churn connections and reset a `:memory:` DB — build it
# once and reuse it. The test suite resets this between tests (see `tests/conftest.py`).
_default_ledger: OrderLedger | None = None

# Story 3.3: the worker-pool means real concurrent `get_order_ledger()` callers (two order tasks
# could race to build two connections, leaking one). A module-level lock + double-checked locking
# closes the factory race. The factory is sync, so a `threading.Lock` is the right primitive. No
# size-cap here — the ledger is an append/upsert audit trail (`INSERT OR REPLACE` keeps it
# ~1 row/order).
_factory_lock = threading.Lock()

_FALSEY = {"0", "false", "no", "off"}


def get_order_ledger() -> OrderLedger:
    """Return the memoised default `OrderLedger` (production enabled; degrade to `NullLedger`).

    Reads `PROOV_LEDGER_ENABLED` (default ENABLED; `0`/`false`/`no`/`off`, case-insensitive →
    `NullLedger`) and `PROOV_LEDGER_PATH` (default `"proov_ledger.db"`, gitignored via `*.db`).
    A failure to open the DB logs a warning and memoises a `NullLedger` (degrade — the ledger
    must never crash the engine).

    Story 3.3: built under `_factory_lock` with double-checked locking so concurrent callers
    (the worker-pool's order tasks) cannot build two connections and leak one.
    """
    global _default_ledger
    if _default_ledger is not None:
        return _default_ledger

    with _factory_lock:
        # Double-checked: another thread may have built the singleton while we waited for the lock.
        if _default_ledger is not None:
            return _default_ledger

        enabled = os.environ.get("PROOV_LEDGER_ENABLED", "1").strip().lower()
        if enabled in _FALSEY:
            _default_ledger = NullLedger()
            return _default_ledger

        try:
            _default_ledger = SqliteOrderLedger(
                os.environ.get("PROOV_LEDGER_PATH", "proov_ledger.db")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Order ledger unavailable, ledger disabled: %r", exc)
            _default_ledger = NullLedger()
        return _default_ledger


def reset_order_ledger() -> None:
    """Close + clear the memoised default ledger (test seam — `tests/conftest.py` autouse)."""
    global _default_ledger
    if _default_ledger is not None:
        close = getattr(_default_ledger, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Order ledger reset close failed: %r", exc)
    _default_ledger = None

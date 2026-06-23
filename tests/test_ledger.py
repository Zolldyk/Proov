"""Tests for the best-effort SQLite order ledger (`proov/ledger.py`) — OFFLINE ONLY.

Real `sqlite3` against `:memory:` / `tmp_path`, no network, no `list_orders`, no wall-clock
waits, no API spend. The autouse fixture in `conftest.py` keeps the default ledger disabled
suite-wide; these tests construct the ledger classes directly. Mirrors `tests/test_cache.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from proov.ledger import (
    NullLedger,
    OrderLedger,
    SqliteOrderLedger,
    get_order_ledger,
    reset_order_ledger,
)
from proov.metrics import OrderRecord

# --------------------------------------------------------------------------- helpers


def _rec(order_id: str = "o1", status: str = "delivering", **kw) -> OrderRecord:
    base = dict(
        order_id=order_id,
        status=status,
        tier="quick",
        requester_agent_id="buyer-1",
        requester_wallet_address="0xwallet",
        provider_agent_id="proov",
        price_usd=0.10,
        cost_usd=0.0,
    )
    base.update(kw)
    return OrderRecord(**base)


# --------------------------------------------------------------------------- round-trip


async def test_record_then_all_orders_round_trips():
    ledger = SqliteOrderLedger(":memory:")
    rec = _rec()
    await ledger.record(rec)
    got = await ledger.all_orders()
    assert got == [rec]


async def test_round_trips_over_a_file(tmp_path):
    ledger = SqliteOrderLedger(str(tmp_path / "ledger.db"))
    await ledger.record(_rec("a"))
    await ledger.record(_rec("b", requester_agent_id="buyer-2"))
    got = {r.order_id for r in await ledger.all_orders()}
    assert got == {"a", "b"}


async def test_preserves_none_price_and_cost():
    ledger = SqliteOrderLedger(":memory:")
    rec = _rec("nullcost", price_usd=None, cost_usd=None)
    await ledger.record(rec)
    (got,) = await ledger.all_orders()
    assert got.price_usd is None
    assert got.cost_usd is None


# --------------------------------------------------------------------------- upsert


async def test_insert_or_replace_updates_in_place_no_duplicate():
    ledger = SqliteOrderLedger(":memory:")
    await ledger.record(_rec("o1", status="delivering"))
    # Re-record the SAME order_id with a newer status — must update, not duplicate.
    await ledger.record(_rec("o1", status="completed"))
    rows = await ledger.all_orders()
    assert len(rows) == 1
    assert rows[0].status == "completed"


# --------------------------------------------------------------------------- best-effort degrade


async def test_record_degrades_to_noop_on_db_error():
    ledger = SqliteOrderLedger(":memory:")
    # Force a broken connection: a closed conn makes every op raise sqlite3.Error.
    ledger._conn.close()
    # Must NOT raise — best-effort no-op (a paid order must never fail on the ledger).
    await ledger.record(_rec("o1"))


async def test_all_orders_degrades_to_empty_on_db_error():
    ledger = SqliteOrderLedger(":memory:")
    await ledger.record(_rec("o1"))
    ledger._conn.close()
    # A read failure degrades to [] rather than raising.
    assert await ledger.all_orders() == []


# --------------------------------------------------------------------------- NullLedger


async def test_null_ledger_is_a_noop():
    ledger = NullLedger()
    assert isinstance(ledger, OrderLedger)
    await ledger.record(_rec("o1"))
    assert await ledger.all_orders() == []


# --------------------------------------------------------------------------- factory


def test_get_order_ledger_disabled_returns_null(monkeypatch):
    monkeypatch.setenv("PROOV_LEDGER_ENABLED", "0")
    reset_order_ledger()
    try:
        ledger = get_order_ledger()
        assert isinstance(ledger, NullLedger)
    finally:
        reset_order_ledger()


def test_get_order_ledger_memoised(monkeypatch):
    monkeypatch.setenv("PROOV_LEDGER_ENABLED", "1")
    monkeypatch.setenv("PROOV_LEDGER_PATH", ":memory:")
    reset_order_ledger()
    try:
        first = get_order_ledger()
        second = get_order_ledger()
        assert first is second
        assert isinstance(first, SqliteOrderLedger)
    finally:
        reset_order_ledger()


def test_reset_order_ledger_clears_singleton(monkeypatch):
    monkeypatch.setenv("PROOV_LEDGER_ENABLED", "1")
    monkeypatch.setenv("PROOV_LEDGER_PATH", ":memory:")
    reset_order_ledger()
    first = get_order_ledger()
    reset_order_ledger()
    second = get_order_ledger()
    try:
        assert first is not second  # a fresh instance after reset
    finally:
        reset_order_ledger()


@pytest.mark.parametrize("flag", ["0", "false", "no", "off", "OFF", "False"])
def test_falsey_values_disable_ledger(monkeypatch, flag):
    monkeypatch.setenv("PROOV_LEDGER_ENABLED", flag)
    reset_order_ledger()
    try:
        assert isinstance(get_order_ledger(), NullLedger)
    finally:
        reset_order_ledger()


# --------------------------------------------------------------------------- factory lock (3.3)


async def test_factory_double_checked_lock_builds_one_ledger(monkeypatch):
    # Concurrent `get_order_ledger()` calls (the worker-pool's order tasks) build EXACTLY ONE
    # ledger under the double-checked `threading.Lock` — no two connections, no leaked one.
    monkeypatch.setenv("PROOV_LEDGER_ENABLED", "1")
    monkeypatch.setenv("PROOV_LEDGER_PATH", ":memory:")
    reset_order_ledger()
    try:
        results = await asyncio.gather(
            *[asyncio.to_thread(get_order_ledger) for _ in range(8)]
        )
        assert all(led is results[0] for led in results)  # one shared singleton
        assert isinstance(results[0], SqliteOrderLedger)
    finally:
        reset_order_ledger()

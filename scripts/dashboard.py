"""Metrics + counter-metric dashboard runner — Proov's success/health numbers (Story 3.2).

OPS / DEV TOOLING, not product. It surfaces the six AC metrics — orders, unique buyer wallets,
unique counterparties, completion rate, **self-trade ratio**, cost/order — plus the PRD §1
targets and a per-target ✓/✗/— marker. Two modes, mirroring `scripts/calibrate.py`:

  * **Offline (default, $0, no network — the safe default).** `python scripts/dashboard.py`
    reads ONLY the local SQLite ledger (`proov/ledger.py` `get_order_ledger().all_orders()`),
    resolves the self/companion agent ids from `PROOV_OWN_AGENT_IDS`, computes the pure metrics
    (`proov/metrics.py` `compute_metrics`) and prints a text dashboard. The ledger's status is a
    delivery-time snapshot (`delivering`), so offline numbers are honest for everything except
    the async `delivering → completed` settlement lag.

  * **`--live` (real `list_orders`, operator-only — needs keys; never run by the suite).**
    `python scripts/dashboard.py --live` reads the **authoritative** order truth via
    `AgentClient.list_orders(role="provider")` and **reconciles** it with the local ledger by
    `order_id`: **live status WINS** (it sees the async `completed` Proov never observes at
    delivery time), the **ledger supplies `tier` + `cost_usd`** (the live `Order` has no such
    field). A live-only order with no ledger row shows `tier="?"`, `cost=None`.

This script is NEVER imported by `proov/provider.py` (no engine/SDK coupling is added to the hot
path) and `--live` is NEVER exercised by the test suite.

Run from the repo root:
    python scripts/dashboard.py            # offline ledger-only dashboard ($0, no keys)
    python scripts/dashboard.py --live     # reconcile real list_orders with the ledger (keys)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Allow `python scripts/dashboard.py` from the repo root to import the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proov.ledger import get_order_ledger  # noqa: E402
from proov.metrics import (  # noqa: E402
    AdoptionGoals,
    AdoptionScore,
    MetricsReport,
    OrderRecord,
    compute_metrics,
    resolve_own_agent_ids,
    score_adoption,
)

log = logging.getLogger("dashboard")

# PRD §1 / Story 4.4 success targets — the SINGLE source of truth is `AdoptionGoals` (the pure
# scorer's bar), so the dashboard markers and the adoption verdict can never silently drift.
_GOALS = AdoptionGoals()
_TARGET_ORDERS = _GOALS.min_completed
_TARGET_EXTERNAL_WALLETS = _GOALS.min_external_wallets
_TARGET_COUNTERPARTIES = _GOALS.min_external_counterparties
_TARGET_COMPLETION_RATE = 0.95  # not part of the 4.4 adoption bar — dashboard-only.


def _pct(value: float | None) -> str:
    """Render a ratio as a 1-dp percentage, or `n/a` when undefined (`None`)."""
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _usd(value: float) -> str:
    """Render a USD amount to 4 dp (sub-cent costs stay visible)."""
    return f"${value:.4f}"


def _mark(value: float | None, threshold: float) -> str:
    """A ✓/✗/— marker: met / not-met / undefined (no data → `None`)."""
    if value is None:
        return "—"
    return "✓" if value >= threshold else "✗"


def render_dashboard(report: MetricsReport) -> str:
    """Format a `MetricsReport` as a readable success + counter-metric text dashboard.

    Pure string formatting (no I/O). `None` ratios render as `n/a` (never a misleading `0%`),
    and each PRD §1 target carries a ✓ (met) / ✗ (missed) / — (no data) marker.
    """
    margin = report.total_revenue_usd - report.total_cost_usd
    lines = [
        "Proov — order metrics + counter-metric dashboard",
        "=" * 48,
        "",
        "Success metrics                                  PRD target",
        f"  total orders          : {report.total_orders:<10d} >= {_TARGET_ORDERS:<5d} "
        f"{_mark(report.total_orders, _TARGET_ORDERS)}",
        f"  completed             : {report.completed_orders:<10d}",
        f"  completion rate       : {_pct(report.completion_rate):<10} >= "
        f"{int(_TARGET_COMPLETION_RATE * 100)}%  "
        f"{_mark(report.completion_rate, _TARGET_COMPLETION_RATE)}",
        f"  unique buyer wallets  : {report.unique_buyer_wallets:<10d}",
        f"    of which external   : {report.unique_external_buyer_wallets:<10d} >= "
        f"{_TARGET_EXTERNAL_WALLETS:<5d} "
        f"{_mark(report.unique_external_buyer_wallets, _TARGET_EXTERNAL_WALLETS)}",
        f"  unique counterparties : {report.unique_counterparties:<10d}",
        f"    of which external   : {report.unique_external_counterparties:<10d} >= "
        f"{_TARGET_COUNTERPARTIES:<5d} "
        f"{_mark(report.unique_external_counterparties, _TARGET_COUNTERPARTIES)}",
        "",
        "Counter-metrics (guard the guardrails — PRD §1: don't win wrong)",
        f"  self-trade ratio      : {_pct(report.self_trade_ratio):<10} "
        f"({report.self_trade_orders}/{report.total_orders} own; external must dominate)",
        f"  cost / order          : "
        f"{(_usd(report.cost_per_order) if report.cost_per_order is not None else 'n/a'):<10} "
        "(must stay ~$0 marginal — NFR1)",
        f"  revenue               : {_usd(report.total_revenue_usd)}",
        f"  margin (rev - cost)   : {_usd(margin)}",
    ]
    return "\n".join(lines)


def _goal_mark(met: bool) -> str:
    """A ✓ / ✗ for a single adoption goal's met / not-met boolean."""
    return "✓" if met else "✗"


def render_adoption(
    score: AdoptionScore, report: MetricsReport, goals: AdoptionGoals
) -> str:
    """Format the Story 4.4 adoption scorecard — per-goal ✓/✗ + the single PASS/FAIL verdict.

    Pure string formatting (no I/O), the twin of `render_dashboard`. Each goal shows its actual
    vs target with a ✓ (met) / ✗ (not met) mark from the pure `AdoptionScore`; the external-
    dominant row shows the self-trade ratio (`n/a` when undefined → ✗). The final
    `ADOPTION GOAL: PASS|FAIL` line is the single "external-dominant adoption goal met" verdict a
    judge (and the operator) reads — FAIL on an empty / self-dominated ledger, never a misleading
    pass.
    """
    lines = [
        "",
        "Adoption goal (Story 4.4 — external-dominant order bar)",
        "-" * 48,
        f"  completed orders       : {report.completed_orders:<10d} >= "
        f"{goals.min_completed:<5d} {_goal_mark(score.completed_met)}",
        f"  external buyer wallets : {report.unique_external_buyer_wallets:<10d} >= "
        f"{goals.min_external_wallets:<5d} {_goal_mark(score.external_wallets_met)}",
        f"  external counterparties: {report.unique_external_counterparties:<10d} >= "
        f"{goals.min_external_counterparties:<5d} {_goal_mark(score.external_counterparties_met)}",
        f"  external-dominant      : self-trade {_pct(report.self_trade_ratio):<8} <  "
        f"{_pct(goals.max_self_trade_ratio):<6} {_goal_mark(score.external_dominant_met)}",
        "",
        f"  ADOPTION GOAL: {'PASS' if score.met else 'FAIL'}"
        "   (external orders must dominate — self-trading is not a win)",
    ]
    return "\n".join(lines)


def _run_offline() -> int:
    """Offline (default): read the local ledger only, compute, print. No network, $0."""

    async def _go() -> MetricsReport:
        records = await get_order_ledger().all_orders()
        own = resolve_own_agent_ids()
        log.info("offline dashboard over %d ledger order(s)", len(records))
        return compute_metrics(records, own_agent_ids=own)

    report = asyncio.run(_go())
    score = score_adoption(report, _GOALS)
    print(render_dashboard(report))
    print(render_adoption(score, report, _GOALS))
    return 0


def _parse_price(raw: str) -> float | None:
    """Scale a base-units USDC price string to USD (`/1e6`); `None` if missing/non-numeric.

    A non-finite (inf/nan) or negative price is garbage that would poison revenue/margin, so it
    degrades to `None` too — the same finite guard `metrics.estimate_order_cost` applies to cost.
    """
    try:
        value = float(raw) / 1_000_000
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


def _reconcile(live_orders: list, ledger_records: list[OrderRecord]) -> list[OrderRecord]:
    """Join live `Order`s with ledger rows by `order_id`: live status wins, ledger gives tier/cost.

    For an order in BOTH: counterparty/wallet/price/status come from the authoritative live
    `Order`, while `tier` + `cost_usd` come from the ledger (the live `Order` has no such field).
    A live-only order (no ledger row) shows `tier="?"`, `cost=None`. A ledger-only order (not in
    the live page — e.g. paged out, or a very old row) is carried through as-is.
    """
    ledger_by_id = {r.order_id: r for r in ledger_records}
    merged: dict[str, OrderRecord] = {}
    for lo in live_orders:
        order_id = getattr(lo, "order_id", "") or ""
        led = ledger_by_id.get(order_id)
        merged[order_id] = OrderRecord(
            order_id=order_id,
            status=getattr(lo, "status", "") or "",  # live status WINS
            tier=led.tier if led else "?",  # ledger supplies tier
            requester_agent_id=getattr(lo, "requester_agent_id", "") or "",
            requester_wallet_address=getattr(lo, "requester_wallet_address", "") or "",
            provider_agent_id=getattr(lo, "provider_agent_id", "") or "",
            price_usd=_parse_price(getattr(lo, "price", "")),
            cost_usd=led.cost_usd if led else None,  # ledger supplies cost
        )
    for order_id, led in ledger_by_id.items():
        if order_id not in merged:
            merged[order_id] = led
    return list(merged.values())


async def _list_all_orders(client) -> list:
    """Page through `list_orders(role="provider")` and return every order."""
    from croo import ListOptions

    page_size = 100
    page = 1
    all_orders: list = []
    while True:
        batch = await client.list_orders(
            ListOptions(role="provider", page=page, page_size=page_size)
        )
        all_orders.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return all_orders


def _run_live() -> int:
    """`--live`: reconcile real `list_orders` with the ledger (live status wins), compute, print."""
    from croo import AgentClient, Config

    from proov.config import AppConfig

    cfg = AppConfig.from_env()

    async def _go() -> MetricsReport:
        client = AgentClient(
            Config(base_url=cfg.api_url, ws_url=cfg.ws_url), sdk_key=cfg.api_key
        )
        try:
            live_orders = await _list_all_orders(client)
            ledger_records = await get_order_ledger().all_orders()
            log.info(
                "live dashboard: %d order(s) from list_orders reconciled with %d ledger row(s)",
                len(live_orders),
                len(ledger_records),
            )
            merged = _reconcile(live_orders, ledger_records)
            return compute_metrics(merged, own_agent_ids=resolve_own_agent_ids())
        finally:
            await client.close()

    report = asyncio.run(_go())
    score = score_adoption(report, _GOALS)
    print(render_dashboard(report))
    print(render_adoption(score, report, _GOALS))
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    live = "--live" in sys.argv[1:]
    return _run_live() if live else _run_offline()


if __name__ == "__main__":
    sys.exit(main())

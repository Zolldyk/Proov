"""Order/metrics computer `[E]` — a PURE, deterministic success + counter-metric scorer (FR18).

Architecture §2/§3 name `[E]` as carrying, beside the claim→evidence cache (`proov/cache.py`),
*"a local order/metrics ledger mirroring `list_orders` for the success/counter-metric
dashboard"*. This module is the **pure** half of that: the math a human (and the judges) act on
— orders, unique buyer wallets, unique counterparties, completion rate, and the two
**counter-metrics** that catch us "winning wrong" (self-trade ratio, cost/order). The
best-effort SQLite ledger that feeds it is `proov/ledger.py`; the runner is
`scripts/dashboard.py`.

Its structural template is `proov/verdict.py` / `proov/calibration.py`, NOT `cache.py`: the
scoring is **pure deterministic computation** — same inputs ⇒ same numbers on CPython. NO
`croo`, NO `httpx`, NO `sqlite3`, NO network, NO clock/randomness, NO env reads inside
`compute_metrics`. stdlib only (`dataclasses`, `logging`; `os` is used ONLY inside the two
config seams `estimate_order_cost` / `resolve_own_agent_ids`, kept apart from the math — the
same discipline `calibration.py` uses to keep `json` out of its scoring functions).

A ratio whose denominator is **zero is `None`** (undefined — NOT `0.0`), so an empty ledger
never reports a misleading `0%` (the precise mirror of `calibration._ratio`). The cost figure
is the documented free-tier `0.0` via `estimate_order_cost`; REAL per-order cost instrumentation
(+ a ceiling) is Story 3.4 — this module only makes the $0-marginal claim *visible and
falsifiable*.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("proov.metrics")

# Terminal vs in-flight order statuses. These are the literal `croo.types.OrderStatus` string
# values (see `croo/types.py:113-126`) — inlined deliberately so this pure module imports NO
# `croo` (architecture §2: the provider is the only CROO-coupled module). `completed/rejected/
# expired/deliver_failed/pay_failed/create_failed` are terminal; everything else
# (`creating/created/paying/paid/delivering/rejecting`) is in-flight and excluded from the
# completion-rate denominator so a freshly-`delivering` order does not depress the rate.
_TERMINAL_STATUSES = frozenset(
    {"completed", "rejected", "expired", "deliver_failed", "pay_failed", "create_failed"}
)


@dataclass(frozen=True)
class OrderRecord:
    """One order's facts as the metrics need them — frozen, pure-value (style of `proov/types.py`).

    Carries the union of what the two truth sources know: `status` (authoritative from a live
    `list_orders` read; a stale delivery-time snapshot in the offline ledger), `tier` +
    `cost_usd` (Proov's own facts the SDK `Order` has no field for), and the
    counterparty/wallet/price the dashboard groups on. `price_usd` / `cost_usd` are `float | None`
    (None = unknown, distinct from a real `0.0`). Sensible empty/`None` defaults so a partial
    row from either source still constructs.
    """

    order_id: str = ""
    status: str = ""
    tier: str = ""
    requester_agent_id: str = ""
    requester_wallet_address: str = ""
    provider_agent_id: str = ""
    price_usd: float | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class MetricsReport:
    """The computed success + counter-metric result vocabulary (frozen, like `Verdict`).

    Success metrics: `total_orders`, `completed_orders`, `completion_rate`,
    `unique_buyer_wallets`, `unique_external_buyer_wallets`, `unique_counterparties`.
    Counter-metrics: `self_trade_orders` / `self_trade_ratio`, `total_cost_usd` / `cost_per_order`.
    `total_revenue_usd` is reported so the operator sees margin = revenue − cost. Every ratio is
    `float | None` — `None` when its denominator is 0 (undefined, never a misleading `0.0`).
    """

    total_orders: int
    completed_orders: int
    completion_rate: float | None
    unique_buyer_wallets: int
    unique_external_buyer_wallets: int
    unique_counterparties: int
    self_trade_orders: int
    self_trade_ratio: float | None
    total_cost_usd: float
    cost_per_order: float | None
    total_revenue_usd: float


def _ratio(numerator: float, denominator: float) -> float | None:
    """Exact `numerator/denominator`, or `None` when the denominator is 0 (undefined).

    The precise mirror of `proov.calibration._ratio`: a zero-denominator ratio is *undefined*,
    not `0.0`, so an empty ledger never reports a misleading 0% completion or $0/order.
    """
    return numerator / denominator if denominator else None


def compute_metrics(
    orders: list[OrderRecord], *, own_agent_ids: frozenset[str]
) -> MetricsReport:
    """Roll `OrderRecord`s into a `MetricsReport` — PURE, deterministic, exact arithmetic (AC2).

    Single pass. The EXACT definitions (no ambiguity for a re-run):

    - `total_orders` = `len(orders)`.
    - `completion_rate` = `completed_orders / terminal_orders` where `completed_orders` counts
      `status == "completed"` and `terminal_orders` counts statuses in `_TERMINAL_STATUSES`;
      `None` if `terminal_orders == 0` (in-flight orders are excluded from the denominator).
    - `unique_buyer_wallets` = distinct non-empty `requester_wallet_address`;
      `unique_external_buyer_wallets` = same, excluding rows whose `requester_agent_id` is in
      `own_agent_ids`; `unique_counterparties` = distinct non-empty `requester_agent_id`.
    - `self_trade_ratio` = `self_trade_orders / total_orders` (own-agent rows / all),
      `None` if `total_orders == 0`.
    - `cost_per_order` = `total_cost_usd / total_orders` (`total_cost_usd` sums non-`None`
      `cost_usd`), `None` if `total_orders == 0`. `total_revenue_usd` sums non-`None` `price_usd`.

    NO I/O, NO clock, NO randomness, NO env read — same inputs ⇒ same numbers on CPython.
    """
    total_orders = len(orders)

    completed_orders = 0
    terminal_orders = 0
    self_trade_orders = 0
    total_cost_usd = 0.0
    total_revenue_usd = 0.0
    buyer_wallets: set[str] = set()
    external_buyer_wallets: set[str] = set()
    counterparties: set[str] = set()

    for o in orders:
        if o.status == "completed":
            completed_orders += 1
        if o.status in _TERMINAL_STATUSES:
            terminal_orders += 1

        is_self = o.requester_agent_id in own_agent_ids
        if is_self:
            self_trade_orders += 1

        wallet = o.requester_wallet_address
        if wallet:
            buyer_wallets.add(wallet)
            if not is_self:
                external_buyer_wallets.add(wallet)
        agent = o.requester_agent_id
        if agent:
            counterparties.add(agent)

        if o.cost_usd is not None:
            total_cost_usd += o.cost_usd
        if o.price_usd is not None:
            total_revenue_usd += o.price_usd

    return MetricsReport(
        total_orders=total_orders,
        completed_orders=completed_orders,
        completion_rate=_ratio(completed_orders, terminal_orders),
        unique_buyer_wallets=len(buyer_wallets),
        unique_external_buyer_wallets=len(external_buyer_wallets),
        unique_counterparties=len(counterparties),
        self_trade_orders=self_trade_orders,
        self_trade_ratio=_ratio(self_trade_orders, total_orders),
        total_cost_usd=total_cost_usd,
        cost_per_order=_ratio(total_cost_usd, total_orders),
        total_revenue_usd=total_revenue_usd,
    )


def estimate_order_cost(tier: str) -> float:
    """The documented per-order marginal cost for `tier` — the falsifiable $0-claim seam (AC5).

    Returns a **documented constant**, default `0.0` on the free-tier stack (Gemini/Tavily/
    Wikipedia free quotas → $0 marginal, NFR1), overridable via `PROOV_DEEP_COST_USD` for
    `"deep"` / `PROOV_QUICK_COST_USD` otherwise for when a tier moves to paid quota. A
    non-numeric override falls back to `0.0` (the defensive parse idiom of
    `cache._resolve_ttl_seconds`). This makes cost/order *visible* today and leaves a clean seam
    for Story 3.4 to replace the estimate with a measured per-order counter (+ a ceiling).
    """
    var = "PROOV_DEEP_COST_USD" if tier == "deep" else "PROOV_QUICK_COST_USD"
    raw = os.environ.get(var, "0.0")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Reject non-finite (inf/nan) overrides too — a garbage cost must never poison cost/order.
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return value


def resolve_own_agent_ids(raw: str | None = None) -> frozenset[str]:
    """Parse the self/companion agent ids that mark a self-trade order (AC2 counter-metric).

    Reads `PROOV_OWN_AGENT_IDS` (comma-separated) when `raw` is not given; strips each id and
    drops empties → a `frozenset`. `None`/empty → empty frozenset (honest: no companion orders
    until the Research caller's id is minted in Epic 4.2 — shipping the seam now means 4.2 only
    sets an env var).
    """
    if raw is None:
        raw = os.environ.get("PROOV_OWN_AGENT_IDS")
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())

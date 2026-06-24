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
    `unique_buyer_wallets`, `unique_external_buyer_wallets`, `unique_counterparties`,
    `unique_external_counterparties`.
    Counter-metrics: `self_trade_orders` / `self_trade_ratio`, `total_cost_usd` / `cost_per_order`.
    `total_revenue_usd` is reported so the operator sees margin = revenue − cost. Every ratio is
    `float | None` — `None` when its denominator is 0 (undefined, never a misleading `0.0`).

    `unique_external_counterparties` (Story 4.4) is the distinct-counterparty count EXCLUDING the
    self/companion agent ids — the honest twin of `unique_external_buyer_wallets`. The all-inclusive
    `unique_counterparties` includes the companion (Story 4.2), so the adoption bar's "≥3 unique
    counterparty *agents*, external-dominant" must score on the external subset (a single
    self/companion agent could otherwise inflate the count — the anti-self-trade point of 4.4).
    """

    total_orders: int
    completed_orders: int
    completion_rate: float | None
    unique_buyer_wallets: int
    unique_external_buyer_wallets: int
    unique_counterparties: int
    unique_external_counterparties: int
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
      `own_agent_ids`; `unique_counterparties` = distinct non-empty `requester_agent_id`;
      `unique_external_counterparties` = same, excluding `requester_agent_id` in `own_agent_ids`.
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
    external_counterparties: set[str] = set()

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
            if not is_self:
                external_counterparties.add(agent)

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
        unique_external_counterparties=len(external_counterparties),
        self_trade_orders=self_trade_orders,
        self_trade_ratio=_ratio(self_trade_orders, total_orders),
        total_cost_usd=total_cost_usd,
        cost_per_order=_ratio(total_cost_usd, total_orders),
        total_revenue_usd=total_revenue_usd,
    )


@dataclass(frozen=True)
class AdoptionGoals:
    """The exact Story 4.4 reward bar as overridable thresholds (frozen value object).

    Defaults encode the epic AC verbatim: ≥10 completed orders, ≥5 unique EXTERNAL buyer
    wallets, ≥3 unique EXTERNAL counterparties, and **external-dominant** (`self_trade_ratio <
    0.5` — strictly more external than self orders). `max_self_trade_ratio` is a strict upper
    bound on the self-trade share; lower it (e.g. `0.2`) to demand the companion stay a small
    minority. Pure data — `score_adoption` reads these, never the env.
    """

    min_completed: int = 10
    min_external_wallets: int = 5
    min_external_counterparties: int = 3
    max_self_trade_ratio: float = 0.5


@dataclass(frozen=True)
class AdoptionScore:
    """The per-goal + overall PASS/FAIL result of scoring a `MetricsReport` against the bar.

    Each `*_met` flag is the individual goal's verdict; `met` is `all(...)` of them — a single
    unambiguous "adoption goal met" boolean the dashboard/judge reads. An empty or
    self-dominated ledger yields `external_dominant_met=False` (a `None` `self_trade_ratio` is
    NOT met — undefined is never a misleading pass), so the overall verdict is honestly FAIL.
    """

    completed_met: bool
    external_wallets_met: bool
    external_counterparties_met: bool
    external_dominant_met: bool
    met: bool


def score_adoption(
    report: MetricsReport, goals: AdoptionGoals = AdoptionGoals()
) -> AdoptionScore:
    """Score a `MetricsReport` against the 4.4 adoption bar → `AdoptionScore` — PURE (AC1/AC3).

    Each count goal is a `>=` against the report's EXTERNAL metrics (wallets/counterparties
    exclude self/companion ids, AC2). External-dominant is `self_trade_ratio is not None and
    self_trade_ratio < goals.max_self_trade_ratio`: a `None` ratio — an empty ledger — is **not
    met** (mirrors `_ratio`'s undefined-on-zero discipline; an empty ledger is FAIL, never a
    misleading pass, AC3), and clearing the raw counts by self-trading (`ratio >= 0.5`) is still
    a FAIL. Overall `met = all` per-goal flags. No I/O, no clock, no randomness, no env read —
    same inputs ⇒ same result on CPython.
    """
    completed_met = report.completed_orders >= goals.min_completed
    external_wallets_met = (
        report.unique_external_buyer_wallets >= goals.min_external_wallets
    )
    external_counterparties_met = (
        report.unique_external_counterparties >= goals.min_external_counterparties
    )
    external_dominant_met = (
        report.self_trade_ratio is not None
        and report.self_trade_ratio < goals.max_self_trade_ratio
    )
    return AdoptionScore(
        completed_met=completed_met,
        external_wallets_met=external_wallets_met,
        external_counterparties_met=external_counterparties_met,
        external_dominant_met=external_dominant_met,
        met=(
            completed_met
            and external_wallets_met
            and external_counterparties_met
            and external_dominant_met
        ),
    )


def estimate_order_cost(tier: str) -> float:
    """The documented per-order marginal cost for `tier` — the falsifiable $0-claim seam (AC5).

    Returns a **documented constant**, default `0.0` on the free-tier stack (Gemini/Tavily/
    Wikipedia free quotas → $0 marginal, NFR1), overridable via `PROOV_DEEP_COST_USD` for
    `"deep"` / `PROOV_QUICK_COST_USD` otherwise for when a tier moves to paid quota. A
    non-numeric override falls back to `0.0` (the defensive parse idiom of
    `cache._resolve_ttl_seconds`).

    This is the **recorded/displayed** per-order figure on the ledger + dashboard. As of
    Story 3.4 the *enforcement* twin exists — `estimate_claim_cost(tier)` drives the engine's
    `PROOV_MAX_ORDER_COST_USD` per-order cost ceiling (the spend-twin of the SLA deadline) —
    but the two remain independent documented constants the operator keeps consistent. The full
    unification into ONE measured per-order counter feeding BOTH the recorded figure and the
    ceiling is Story 3.4 Open Question 2 (it would need `Report` to carry a non-hashed measured
    cost; out of scope here).
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


def estimate_claim_cost(tier: str) -> float:
    """The documented per-claim marginal cost for `tier` — the cost ceiling's meter (Story 3.4).

    The per-claim spend estimate the engine's `PROOV_MAX_ORDER_COST_USD` ceiling accumulates
    against (one `estimate_claim_cost(tier)` per judged claim slice). Default `0.0` on the
    free-tier stack (→ the ceiling meter is inert, the $0 path unchanged, NFR1), overridable
    via `PROOV_DEEP_CLAIM_COST_USD` for `"deep"` / `PROOV_QUICK_CLAIM_COST_USD` otherwise for
    when a tier moves to paid quota. **Identical defensive shape to `estimate_order_cost`:** a
    non-numeric or non-finite (`inf`/`nan`) value falls back to `0.0` so garbage can never
    poison the meter (which would spuriously stop an order early). The per-order
    `estimate_order_cost` remains the recorded/displayed figure; see its docstring + OQ2.
    """
    var = "PROOV_DEEP_CLAIM_COST_USD" if tier == "deep" else "PROOV_QUICK_CLAIM_COST_USD"
    raw = os.environ.get(var, "0.0")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Reject non-finite (inf/nan) overrides too — garbage must never poison the cost meter.
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

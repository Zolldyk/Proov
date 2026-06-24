"""Tests for the pure order-metrics computer (`proov/metrics.py`) — OFFLINE, SYNC.

Hand-built `OrderRecord` lists; no network, no `list_orders`, no SQLite, no clock. Mirrors the
pure-math unit style of `tests/test_calibration.py`.
"""

from __future__ import annotations

from proov.metrics import (
    MetricsReport,
    OrderRecord,
    compute_metrics,
    estimate_claim_cost,
    estimate_order_cost,
    resolve_own_agent_ids,
)

# --------------------------------------------------------------------------- helpers


def _order(
    order_id: str = "o1",
    status: str = "completed",
    tier: str = "quick",
    requester_agent_id: str = "agent-ext",
    requester_wallet_address: str = "0xwallet",
    provider_agent_id: str = "proov",
    price_usd: float | None = None,
    cost_usd: float | None = None,
) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        status=status,
        tier=tier,
        requester_agent_id=requester_agent_id,
        requester_wallet_address=requester_wallet_address,
        provider_agent_id=provider_agent_id,
        price_usd=price_usd,
        cost_usd=cost_usd,
    )


_NO_OWN: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- empty / zero


def test_empty_orders_report_is_all_zero_with_none_ratios():
    report = compute_metrics([], own_agent_ids=_NO_OWN)
    assert isinstance(report, MetricsReport)
    assert report.total_orders == 0
    assert report.completed_orders == 0
    # Every ratio is None on a zero denominator — never a misleading 0.0/0%.
    assert report.completion_rate is None
    assert report.self_trade_ratio is None
    assert report.cost_per_order is None
    assert report.unique_buyer_wallets == 0
    assert report.total_cost_usd == 0.0
    assert report.total_revenue_usd == 0.0


# --------------------------------------------------------------------------- completion rate


def test_completion_rate_excludes_in_flight_from_denominator():
    # 9 completed + 1 rejected (terminal) + 2 delivering (in-flight) → 9/10 = 0.90.
    orders = (
        [_order(f"c{i}", status="completed") for i in range(9)]
        + [_order("r1", status="rejected")]
        + [_order("d1", status="delivering"), _order("d2", status="delivering")]
    )
    report = compute_metrics(orders, own_agent_ids=_NO_OWN)
    assert report.total_orders == 12
    assert report.completed_orders == 9
    assert report.completion_rate == 0.90


def test_completion_rate_none_when_only_in_flight():
    orders = [_order("d1", status="delivering"), _order("p1", status="paid")]
    report = compute_metrics(orders, own_agent_ids=_NO_OWN)
    assert report.completed_orders == 0
    # Zero terminal orders → undefined, NOT 0.0 (a freshly-delivering batch is not "0% complete").
    assert report.completion_rate is None


def test_all_terminal_failure_statuses_count_in_denominator():
    # expired / deliver_failed / pay_failed / create_failed are terminal-but-not-completed.
    orders = [
        _order("c1", status="completed"),
        _order("e1", status="expired"),
        _order("f1", status="deliver_failed"),
        _order("f2", status="pay_failed"),
        _order("f3", status="create_failed"),
    ]
    report = compute_metrics(orders, own_agent_ids=_NO_OWN)
    assert report.completed_orders == 1
    assert report.completion_rate == 1 / 5  # 5 terminal, 1 completed


# --------------------------------------------------------------------------- unique wallets


def test_unique_buyer_wallets_dedupe_and_drop_empty():
    orders = [
        _order("o1", requester_wallet_address="0xA"),
        _order("o2", requester_wallet_address="0xA"),  # dup
        _order("o3", requester_wallet_address="0xB"),
        _order("o4", requester_wallet_address=""),  # empty → dropped
    ]
    report = compute_metrics(orders, own_agent_ids=_NO_OWN)
    assert report.unique_buyer_wallets == 2


def test_external_wallet_split_excludes_own_agent_rows():
    own = frozenset({"companion"})
    orders = [
        _order("o1", requester_agent_id="companion", requester_wallet_address="0xself"),
        _order("o2", requester_agent_id="buyer-1", requester_wallet_address="0xext1"),
        _order("o3", requester_agent_id="buyer-2", requester_wallet_address="0xext2"),
    ]
    report = compute_metrics(orders, own_agent_ids=own)
    assert report.unique_buyer_wallets == 3  # all wallets distinct
    assert report.unique_external_buyer_wallets == 2  # the companion wallet excluded
    assert report.unique_counterparties == 3


# --------------------------------------------------------------------------- self-trade ratio


def test_self_trade_ratio_counts_own_agent_orders():
    own = frozenset({"companion"})
    orders = [_order("o0", requester_agent_id="companion")] + [
        _order(f"o{i}", requester_agent_id="buyer") for i in range(1, 4)
    ]
    report = compute_metrics(orders, own_agent_ids=own)
    assert report.self_trade_orders == 1
    assert report.self_trade_ratio == 0.25  # 1 own / 4 total


def test_self_trade_ratio_none_on_zero_orders():
    report = compute_metrics([], own_agent_ids=frozenset({"companion"}))
    assert report.self_trade_orders == 0
    assert report.self_trade_ratio is None


# --------------------------------------------------------------------------- cost / revenue


def test_cost_per_order_over_mixed_costs():
    # None costs are excluded from the sum; 0.0 and positives are summed.
    orders = [
        _order("o1", cost_usd=None),
        _order("o2", cost_usd=0.0),
        _order("o3", cost_usd=0.30),
        _order("o4", cost_usd=0.10),
    ]
    report = compute_metrics(orders, own_agent_ids=_NO_OWN)
    assert report.total_cost_usd == 0.40
    assert report.cost_per_order == 0.40 / 4  # divided by TOTAL orders (4), not non-None (3)


def test_revenue_and_margin_inputs():
    orders = [
        _order("o1", price_usd=0.10, cost_usd=0.0),
        _order("o2", price_usd=0.50, cost_usd=0.0),
        _order("o3", price_usd=None, cost_usd=None),  # unknown price excluded
    ]
    report = compute_metrics(orders, own_agent_ids=_NO_OWN)
    assert report.total_revenue_usd == 0.60
    assert report.total_cost_usd == 0.0  # margin = revenue - cost = 0.60


# --------------------------------------------------------------------------- estimate_order_cost


def test_estimate_order_cost_defaults_zero(monkeypatch):
    monkeypatch.delenv("PROOV_QUICK_COST_USD", raising=False)
    monkeypatch.delenv("PROOV_DEEP_COST_USD", raising=False)
    assert estimate_order_cost("quick") == 0.0
    assert estimate_order_cost("deep") == 0.0


def test_estimate_order_cost_env_override(monkeypatch):
    monkeypatch.setenv("PROOV_QUICK_COST_USD", "0.02")
    monkeypatch.setenv("PROOV_DEEP_COST_USD", "0.15")
    assert estimate_order_cost("quick") == 0.02
    assert estimate_order_cost("deep") == 0.15


def test_estimate_order_cost_garbage_override_falls_back_to_zero(monkeypatch):
    monkeypatch.setenv("PROOV_QUICK_COST_USD", "not-a-number")
    monkeypatch.setenv("PROOV_DEEP_COST_USD", "inf")
    assert estimate_order_cost("quick") == 0.0
    assert estimate_order_cost("deep") == 0.0


def test_estimate_order_cost_unknown_tier_uses_quick_var(monkeypatch):
    monkeypatch.setenv("PROOV_QUICK_COST_USD", "0.07")
    monkeypatch.delenv("PROOV_DEEP_COST_USD", raising=False)
    # Anything that is not exactly "deep" reads the QUICK var (permissive default).
    assert estimate_order_cost("weird") == 0.07


# --------------------------------------------------------------------------- estimate_claim_cost (Story 3.4)


def test_estimate_claim_cost_defaults_zero(monkeypatch):
    # Default $0 on the free-tier stack → the engine's cost meter is inert (the $0 path).
    monkeypatch.delenv("PROOV_QUICK_CLAIM_COST_USD", raising=False)
    monkeypatch.delenv("PROOV_DEEP_CLAIM_COST_USD", raising=False)
    assert estimate_claim_cost("quick") == 0.0
    assert estimate_claim_cost("deep") == 0.0


def test_estimate_claim_cost_env_override(monkeypatch):
    # Each tier reads its OWN per-claim cost var (distinct from the per-order COST_USD vars).
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.001")
    monkeypatch.setenv("PROOV_DEEP_CLAIM_COST_USD", "0.01")
    assert estimate_claim_cost("quick") == 0.001
    assert estimate_claim_cost("deep") == 0.01


def test_estimate_claim_cost_garbage_or_nonfinite_falls_back_to_zero(monkeypatch):
    # Garbage / inf / nan → 0.0 so a poisoned constant can never spuriously stop an order early.
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "not-a-number")
    monkeypatch.setenv("PROOV_DEEP_CLAIM_COST_USD", "inf")
    assert estimate_claim_cost("quick") == 0.0
    assert estimate_claim_cost("deep") == 0.0
    monkeypatch.setenv("PROOV_DEEP_CLAIM_COST_USD", "nan")
    assert estimate_claim_cost("deep") == 0.0


def test_estimate_claim_cost_unknown_tier_uses_quick_var(monkeypatch):
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.003")
    monkeypatch.delenv("PROOV_DEEP_CLAIM_COST_USD", raising=False)
    # Anything not exactly "deep" reads the QUICK var (mirrors estimate_order_cost).
    assert estimate_claim_cost("weird") == 0.003


# --------------------------------------------------------------------------- resolve_own_agent_ids


def test_resolve_own_agent_ids_parses_csv():
    assert resolve_own_agent_ids("a, b ,c") == frozenset({"a", "b", "c"})


def test_resolve_own_agent_ids_drops_empties():
    assert resolve_own_agent_ids("a,,  ,b") == frozenset({"a", "b"})


def test_resolve_own_agent_ids_none_and_empty():
    assert resolve_own_agent_ids(None) == frozenset()
    assert resolve_own_agent_ids("") == frozenset()


def test_resolve_own_agent_ids_reads_env(monkeypatch):
    monkeypatch.setenv("PROOV_OWN_AGENT_IDS", "companion-1, companion-2")
    assert resolve_own_agent_ids() == frozenset({"companion-1", "companion-2"})

"""Tests for the pure order-metrics computer (`proov/metrics.py`) — OFFLINE, SYNC.

Hand-built `OrderRecord` lists; no network, no `list_orders`, no SQLite, no clock. Mirrors the
pure-math unit style of `tests/test_calibration.py`.
"""

from __future__ import annotations

from dataclasses import replace as dataclasses_replace

from proov.metrics import (
    AdoptionGoals,
    AdoptionScore,
    MetricsReport,
    OrderRecord,
    compute_metrics,
    estimate_claim_cost,
    estimate_order_cost,
    resolve_own_agent_ids,
    score_adoption,
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


# --------------------------------------------------------------------------- unique_external_counterparties (4.4)


def test_unique_external_counterparties_excludes_own_ids():
    # 3 distinct external agents + 1 companion (own) agent placing 2 orders.
    own = frozenset({"companion-1"})
    orders = [
        _order("o1", requester_agent_id="ext-a", requester_wallet_address="0xa"),
        _order("o2", requester_agent_id="ext-b", requester_wallet_address="0xb"),
        _order("o3", requester_agent_id="ext-c", requester_wallet_address="0xc"),
        _order("o4", requester_agent_id="companion-1", requester_wallet_address="0xself"),
        _order("o5", requester_agent_id="companion-1", requester_wallet_address="0xself"),
    ]
    report = compute_metrics(orders, own_agent_ids=own)
    # all-inclusive count sees the companion; the external count does not.
    assert report.unique_counterparties == 4
    assert report.unique_external_counterparties == 3
    assert report.self_trade_orders == 2


def test_unique_external_counterparties_is_zero_when_only_self():
    own = frozenset({"companion-1"})
    orders = [_order("o1", requester_agent_id="companion-1")]
    report = compute_metrics(orders, own_agent_ids=own)
    assert report.unique_external_counterparties == 0


# --------------------------------------------------------------------------- score_adoption (4.4)


def _passing_report() -> MetricsReport:
    """A report that clears all four 4.4 goals (10 completed, 5 ext wallets, 3 ext counterparties,
    external-dominant)."""
    return MetricsReport(
        total_orders=12,
        completed_orders=10,
        completion_rate=1.0,
        unique_buyer_wallets=6,
        unique_external_buyer_wallets=5,
        unique_counterparties=4,
        unique_external_counterparties=3,
        self_trade_orders=2,  # 2/12 ≈ 0.167 < 0.5 → external-dominant
        self_trade_ratio=2 / 12,
        total_cost_usd=0.0,
        cost_per_order=0.0,
        total_revenue_usd=1.2,
    )


def test_score_adoption_passes_when_all_goals_clear():
    score = score_adoption(_passing_report())
    assert isinstance(score, AdoptionScore)
    assert score.completed_met
    assert score.external_wallets_met
    assert score.external_counterparties_met
    assert score.external_dominant_met
    assert score.met is True


def test_score_adoption_fails_when_completed_short():
    report = dataclasses_replace(_passing_report(), completed_orders=9)
    score = score_adoption(report)
    assert score.completed_met is False
    assert score.met is False


def test_score_adoption_fails_when_external_wallets_short():
    report = dataclasses_replace(_passing_report(), unique_external_buyer_wallets=4)
    score = score_adoption(report)
    assert score.external_wallets_met is False
    assert score.met is False


def test_score_adoption_fails_when_external_counterparties_short():
    report = dataclasses_replace(_passing_report(), unique_external_counterparties=2)
    score = score_adoption(report)
    assert score.external_counterparties_met is False
    assert score.met is False


def test_score_adoption_fails_external_dominant_while_raw_counts_clear():
    # The anti-self-trade case: 10 completed / 5 wallets / 3 counterparties all clear, but
    # half-or-more of the orders are self-trade → external-dominant FAILS → overall FAIL.
    report = dataclasses_replace(
        _passing_report(), self_trade_orders=6, self_trade_ratio=6 / 12
    )
    score = score_adoption(report)
    assert score.completed_met
    assert score.external_wallets_met
    assert score.external_counterparties_met
    assert score.external_dominant_met is False  # 0.5 is NOT < 0.5
    assert score.met is False


def test_score_adoption_empty_ledger_is_fail_not_misleading_pass():
    report = compute_metrics([], own_agent_ids=_NO_OWN)
    score = score_adoption(report)
    assert score.completed_met is False
    assert score.external_wallets_met is False
    assert score.external_counterparties_met is False
    # self_trade_ratio is None on an empty ledger → external-dominant is NOT met.
    assert score.external_dominant_met is False
    assert score.met is False


def test_score_adoption_honours_custom_goals():
    # A stricter companion-minority bar: max_self_trade_ratio=0.2 fails a 0.167-ratio report? No,
    # 0.167 < 0.2 still passes; tighten further to prove the override bites.
    report = _passing_report()  # ratio ≈ 0.167
    strict = AdoptionGoals(max_self_trade_ratio=0.1)
    assert score_adoption(report, strict).external_dominant_met is False
    lenient = AdoptionGoals(min_completed=5, min_external_counterparties=1)
    assert score_adoption(dataclasses_replace(report, completed_orders=5), lenient).met is True


def test_adoption_goals_defaults_encode_the_epic_bar():
    g = AdoptionGoals()
    assert (g.min_completed, g.min_external_wallets, g.min_external_counterparties) == (10, 5, 3)
    assert g.max_self_trade_ratio == 0.5

"""Tests for the adoption scorecard rendering in `scripts/dashboard.py` (Story 4.4) — OFFLINE.

`scripts/` is not a package, so the module is loaded by file path. Only the PURE renderer
(`render_adoption`) and the constants-agree invariant are exercised here — the `--live`/offline
order plumbing is operator-only and never run by the suite (Story 3.2 contract). No socket, no
network, no clock.
"""

from __future__ import annotations

import importlib.util
import pathlib

from proov.metrics import AdoptionGoals, MetricsReport, score_adoption

_DASHBOARD_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "dashboard.py"


def _load_dashboard():
    spec = importlib.util.spec_from_file_location("_dashboard_under_test", _DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = _load_dashboard()


def _report(
    *,
    completed_orders: int,
    unique_external_buyer_wallets: int,
    unique_external_counterparties: int,
    self_trade_ratio: float | None,
) -> MetricsReport:
    return MetricsReport(
        total_orders=max(completed_orders, 1),
        completed_orders=completed_orders,
        completion_rate=1.0,
        unique_buyer_wallets=unique_external_buyer_wallets,
        unique_external_buyer_wallets=unique_external_buyer_wallets,
        unique_counterparties=unique_external_counterparties,
        unique_external_counterparties=unique_external_counterparties,
        self_trade_orders=0,
        self_trade_ratio=self_trade_ratio,
        total_cost_usd=0.0,
        cost_per_order=0.0,
        total_revenue_usd=0.0,
    )


def test_render_adoption_pass_block():
    report = _report(
        completed_orders=10,
        unique_external_buyer_wallets=5,
        unique_external_counterparties=3,
        self_trade_ratio=0.1,
    )
    goals = AdoptionGoals()
    out = dashboard.render_adoption(score_adoption(report, goals), report, goals)
    assert "Adoption goal" in out
    assert "ADOPTION GOAL: PASS" in out
    # every per-goal row is ticked
    assert out.count("✓") == 4
    assert "✗" not in out


def test_render_adoption_fail_block_marks_failed_goals():
    # raw counts clear but self-trade dominates → external-dominant ✗ → overall FAIL.
    report = _report(
        completed_orders=10,
        unique_external_buyer_wallets=5,
        unique_external_counterparties=3,
        self_trade_ratio=0.6,
    )
    goals = AdoptionGoals()
    out = dashboard.render_adoption(score_adoption(report, goals), report, goals)
    assert "ADOPTION GOAL: FAIL" in out
    assert "✗" in out  # the external-dominant row failed


def test_render_adoption_empty_ledger_is_fail():
    report = _report(
        completed_orders=0,
        unique_external_buyer_wallets=0,
        unique_external_counterparties=0,
        self_trade_ratio=None,
    )
    report = MetricsReport(
        total_orders=0,
        completed_orders=0,
        completion_rate=None,
        unique_buyer_wallets=0,
        unique_external_buyer_wallets=0,
        unique_counterparties=0,
        unique_external_counterparties=0,
        self_trade_orders=0,
        self_trade_ratio=None,
        total_cost_usd=0.0,
        cost_per_order=None,
        total_revenue_usd=0.0,
    )
    goals = AdoptionGoals()
    out = dashboard.render_adoption(score_adoption(report, goals), report, goals)
    assert "ADOPTION GOAL: FAIL" in out
    # an undefined self-trade ratio renders as n/a, not a misleading 0%.
    assert "n/a" in out


def test_dashboard_targets_derive_from_adoption_goals():
    # The dashboard PRD markers and the scorer bar must not silently drift (single source).
    goals = AdoptionGoals()
    assert dashboard._TARGET_ORDERS == goals.min_completed
    assert dashboard._TARGET_EXTERNAL_WALLETS == goals.min_external_wallets
    assert dashboard._TARGET_COUNTERPARTIES == goals.min_external_counterparties


def test_render_dashboard_shows_external_counterparties_line():
    report = _report(
        completed_orders=4,
        unique_external_buyer_wallets=2,
        unique_external_counterparties=2,
        self_trade_ratio=0.0,
    )
    out = dashboard.render_dashboard(report)
    assert "of which external" in out  # external counterparty line is surfaced

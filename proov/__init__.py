"""Proov — Proof-of-Verification Oracle, a paid callable CAP agent on CROO.

Story 1.2 establishes the first source tree:
- `config`   — AppConfig + .env loader (SDK-agnostic).
- `provider` — `[A]` CAP Provider Adapter: connect, go online, listen, lifecycle.
- `__main__` — `python -m proov` entrypoint.

As of Story 2.6 the engine `[B]` (`engine.verify`) orchestrates the LLM `[C]` (`llm`) and
search `[D]` (`search`) slices into the real single-pass verification pipeline (extract →
retrieve → judge → check-citations → aggregate → deliverable). Story 2.7 lights up the Deep
tier through the SAME orchestration: `tier == "deep"` switches the slices to multi-source
retrieval, multi-pass (self-consistency) judgment and provided+discovered citations under a
wider SLA budget, and the provider uploads a downloadable full report for large deliverables.
As of Story 2.8 the claim→evidence cache `[E]` (`cache`) lands — a TTL'd SQLite cache wired
into `retrieve_evidence` so a repeated claim is served with no new search call. As of Story 3.1
the engine is calibrated to the ≥80%-precision bar (NFR4): `calibration` is a pure, deterministic
scorer measuring verdict precision against a committed hand-labeled set
(`calibration/calibration_set.json`), gated offline by the suite and the `scripts/calibrate.py`
runner; the `fabricated` citation flag is tightened to a definitive 404/410 (precision over
recall). As of Story 3.2 the order/metrics ledger `[E]` lands: `metrics` is a pure, deterministic
computer for the success + counter-metrics (orders, unique buyer wallets, counterparties,
completion rate, self-trade ratio, cost/order); `ledger` is a best-effort SQLite order log the
provider writes at every terminal order (mirroring `cache`'s degrade-don't-drop discipline); and
`scripts/dashboard.py` prints the dashboard offline ($0) or reconciles real `list_orders` with the
ledger under `--live`. As of Story 3.3 the provider is reliability-hardened — a worker-pool throttle
bounds concurrent verifications within free-tier RPM, per-claim/per-order SLA bounds degrade to an
honest `partial` instead of overrunning the wall, and a graceful shutdown drains in-flight
settlements (auto-reconnect itself stays owned by the SDK's `EventStream`). Only `[A]` is
CROO-coupled — `[B]`/`[C]`/`[D]`/`[E]` stay pure, SDK-agnostic Python.
"""

# Single source of the Proov version, stamped into every on-chain receipt (Story 1.4).
# Keep in sync with `pyproject.toml` `[project].version`.
__version__ = "0.1.0"

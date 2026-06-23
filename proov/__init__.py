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
into `retrieve_evidence` so a repeated claim is served with no new search call (the order/metrics
ledger `[E]` arrives in Story 3.2). Only `[A]` is CROO-coupled — `[B]`/`[C]`/`[D]`/`[E]` stay
pure, SDK-agnostic Python.
"""

# Single source of the Proov version, stamped into every on-chain receipt (Story 1.4).
# Keep in sync with `pyproject.toml` `[project].version`.
__version__ = "0.1.0"

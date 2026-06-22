"""Proov — Proof-of-Verification Oracle, a paid callable CAP agent on CROO.

Story 1.2 establishes the first source tree:
- `config`   — AppConfig + .env loader (SDK-agnostic).
- `provider` — `[A]` CAP Provider Adapter: connect, go online, listen, lifecycle.
- `__main__` — `python -m proov` entrypoint.

As of Story 2.6 the engine `[B]` (`engine.verify`) orchestrates the LLM `[C]` (`llm`) and
search `[D]` (`search`) slices into the real single-pass Quick Check pipeline (extract →
retrieve → judge → check-citations → aggregate → deliverable). The Deep tier (multi-pass)
and the cache/ledger `[E]` components arrive in later stories; only `[A]` is CROO-coupled —
`[B]`/`[C]`/`[D]` stay pure, SDK-agnostic Python.
"""

# Single source of the Proov version, stamped into every on-chain receipt (Story 1.4).
# Keep in sync with `pyproject.toml` `[project].version`.
__version__ = "0.1.0"

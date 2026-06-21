"""Proov — Proof-of-Verification Oracle, a paid callable CAP agent on CROO.

Story 1.2 establishes the first source tree:
- `config`   — AppConfig + .env loader (SDK-agnostic).
- `provider` — `[A]` CAP Provider Adapter: connect, go online, listen, lifecycle.
- `__main__` — `python -m proov` entrypoint.

The engine `[B]`, LLM `[C]`, search `[D]` and cache/ledger `[E]` components arrive
in later epics; only `[A]` is CROO-coupled.
"""

# Single source of the Proov version, stamped into every on-chain receipt (Story 1.4).
# Keep in sync with `pyproject.toml` `[project].version`.
__version__ = "0.1.0"

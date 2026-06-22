"""Shared verification-engine value types (pure, SDK-agnostic).

The engine `[B]` is deliberately decoupled from the CROO SDK and from any I/O — this
module is its type vocabulary. It mirrors the pure style of `proov/receipt.py` /
`proov/validation.py` / `proov/services.py`: NO `croo` import, NO `httpx`, no I/O, no
logging side-effects. Only stdlib.

Story 2.1 establishes the first slice: the internal `Claim` (id + text), the `Tier`
literal (the same two strings `proov.services.tier_for_service` returns), the per-tier
claim caps (FR6), and `max_claims_for_tier`. Later engine types (`Evidence`/`Judgment`/
`Report` — Stories 2.2/2.3/2.5) get added here too; they are intentionally NOT added now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# A logical service tier. EXACTLY the two strings `proov.services.tier_for_service`
# returns — keep them in lockstep so the engine (Story 2.6) can pass that result straight
# into `max_claims_for_tier`/`extract_claims`.
Tier = Literal["quick", "deep"]

# Per-tier claim caps (FR6 / PRD §6 / epic 2.1 AC): Quick = 20, Deep = default 50.
QUICK_MAX_CLAIMS = 20
DEEP_MAX_CLAIMS = 50

# Per-tier evidence counts (FR7 / architecture §4): Quick ≈ 1 source/claim → small;
# Deep multi-source → larger. Concrete bounded values keep retrieval cost/latency low
# while giving the judge (Story 2.3) enough to ground a verdict. Option-lowerable below.
QUICK_EVIDENCE_K = 3
DEEP_EVIDENCE_K = 6


@dataclass(frozen=True)
class Claim:
    """A single discrete, checkable factual claim extracted from a submitted output.

    Frozen (immutable, hashable) so a list of Claims can be deduped/compared safely and
    can never be mutated in place once produced. `id` is a positional label (`"c1"`,
    `"c2"`, …) assigned at extraction; `text` is the normalised claim string.
    """

    id: str
    text: str


@dataclass(frozen=True)
class Evidence:
    """A single piece of raw retrieved evidence for a claim (RAG, Story 2.2).

    Frozen (immutable, hashable) like `Claim`, so a list of `Evidence` can be deduped and
    compared safely. This is the **raw retrieved** chunk fed to the judge in Story 2.3 —
    it carries NO `stance`/`supports`/`refutes` field, because stance is a *judgment*
    output assigned during judgment, not a retrieval fact. `source` is the result URL,
    `title` its headline, `snippet` the retrieved text extract, and `score` the provider's
    optional relevance signal (Tavily supplies it; Wikipedia does not). `score` is internal
    retrieval metadata only — it must never be written into a hashed structure here.
    """

    source: str
    title: str
    snippet: str
    score: float | None = None


def max_claims_for_tier(tier: Tier, options: dict | None = None) -> int:
    """Resolve the claim cap for `tier`, allowing a caller `options.max_claims` to LOWER it.

    Base ceiling is 50 for `"deep"`, otherwise 20 (Quick / unknown — permissive default
    matching `tier_for_service`). A PRD §6 `options.max_claims` may reduce the cap below
    the tier ceiling (cost/SLA protection) but can NEVER raise it above. A missing /
    non-int / ≤0 `max_claims` is ignored. The result is the cost-bounded ceiling the
    extractor truncates to.
    """
    base = DEEP_MAX_CLAIMS if tier == "deep" else QUICK_MAX_CLAIMS
    if options is not None:
        requested = options.get("max_claims")
        # `bool` is an `int` subclass — exclude it so a stray True/False can't set a cap.
        if isinstance(requested, int) and not isinstance(requested, bool) and requested >= 1:
            return min(base, requested)
    return base


def evidence_k_for_tier(tier: Tier, options: dict | None = None) -> int:
    """Resolve the evidence count `k` for `tier`, allowing `options` to LOWER it.

    Base is `DEEP_EVIDENCE_K` for `"deep"`, otherwise `QUICK_EVIDENCE_K` (Quick / unknown).
    A PRD §6 `options.max_evidence` (or `options.k`) ≥ 1 may reduce `k` below the tier base
    (cost/SLA protection) but can NEVER raise it above. A missing / non-int (`bool`
    excluded) / ≤0 value is ignored. Mirrors the shape of `max_claims_for_tier`.
    """
    base = DEEP_EVIDENCE_K if tier == "deep" else QUICK_EVIDENCE_K
    if options is not None:
        # `max_evidence` takes precedence, but fall back to `k` when it is absent OR
        # explicitly None (a present-but-None `max_evidence` must not shadow a valid `k`).
        requested = options.get("max_evidence")
        if requested is None:
            requested = options.get("k")
        # `bool` is an `int` subclass — exclude it so a stray True/False can't set k.
        if isinstance(requested, int) and not isinstance(requested, bool) and requested >= 1:
            return min(base, requested)
    return base

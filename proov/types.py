"""Shared verification-engine value types (pure, SDK-agnostic).

The engine `[B]` is deliberately decoupled from the CROO SDK and from any I/O — this
module is its type vocabulary. It mirrors the pure style of `proov/receipt.py` /
`proov/validation.py` / `proov/services.py`: NO `croo` import, NO `httpx`, no I/O, no
logging side-effects. Only stdlib.

Story 2.1 established the first slice (`Claim`, `Tier`, claim caps, `max_claims_for_tier`);
Story 2.2 added `Evidence`/evidence caps; Story 2.3 adds the judgment vocabulary
(`ClaimStatus`, `Stance`, `EvidenceStance`, `Judgment`, `clamp_confidence`). The `Report`
aggregate type (Story 2.5) lands here later. The pure constraint holds throughout: stdlib
only (`math` for the confidence clamp), no `croo`, no `httpx`, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# A logical service tier. EXACTLY the two strings `proov.services.tier_for_service`
# returns — keep them in lockstep so the engine (Story 2.6) can pass that result straight
# into `max_claims_for_tier`/`extract_claims`.
Tier = Literal["quick", "deep"]

# Per-claim judgment label (Story 2.3 / PRD §6). The verdict-aggregation rule (Story 2.5)
# consumes these: a claim is `supported`/`unsupported` against its evidence, or
# `unverifiable` when the evidence is thin (precision over recall — never a guess).
ClaimStatus = Literal["supported", "unsupported", "unverifiable"]

# The judge's read on how a single piece of evidence relates to the claim (PRD §6
# per-claim `evidence[].stance`). `neutral` is the safe fallback for an unknown stance.
Stance = Literal["supports", "refutes", "neutral"]

# Per-source citation-check flag (Story 2.4 / PRD §6 `citations_checked[].flag`). A
# provided source is `ok` (retrievable and either supports the output or support is merely
# unconfirmed), `fabricated` (NOT retrievable — the verdict-flipping flag the Story 2.5
# `fail` rule keys on: FR10 `fail = ≥1 fabricated citation`), or `misattributed`
# (retrievable but a *positively* refuted attachment). Precision over recall (NFR4):
# `fabricated` fires only on a confirmed-unretrievable source, `misattributed` only on a
# positive `unsupported` judgment — never on mere uncertainty.
CitationFlag = Literal["ok", "fabricated", "misattributed"]

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


@dataclass(frozen=True)
class EvidenceStance:
    """A single piece of *judged* evidence backing a claim verdict (PRD §6 `{source, quote, stance}`).

    Frozen (immutable, hashable) like `Evidence`/`Claim`. This is the judgment-layer view of
    evidence — unlike raw `Evidence`, it carries a `stance`, because stance is the judge's
    call, assigned during judgment (Story 2.3), not a retrieval fact. `source` must trace
    back to a retrieved `Evidence.source` (anti-fabrication grounding, enforced in
    `proov.llm._normalise_judgment`); `quote` is the bounded supporting/refuting extract.
    """

    source: str
    quote: str
    stance: Stance


@dataclass(frozen=True)
class Judgment:
    """A per-claim judgment: a label, a calibrated confidence, and the grounding evidence.

    Frozen (immutable, hashable) like `Claim`/`Evidence`. `evidence` is a **tuple** (not a
    list) so `Judgment` stays both frozen *and* hashable — a `list` field would silently
    break hashability. `confidence` is always a `float` (see `clamp_confidence`): it is
    eventually hashed into the report body (Story 2.6) where `0` and `0.0` canonicalise to
    different bytes, so an `int` here would plant a latent hash bug.
    """

    status: ClaimStatus
    confidence: float
    evidence: tuple[EvidenceStance, ...] = ()


@dataclass(frozen=True)
class CitationCheck:
    """A per-source citation-check result (PRD §6 `citations_checked[]`, Story 2.4).

    Frozen (immutable, hashable) like `Claim`/`Evidence`/`EvidenceStance`/`Judgment`. The
    field order/shape is **exactly** the PRD §6 `citations_checked[]` object
    `{source, retrievable, supports_attached_claim, flag}` — `source` is the provided URL,
    `retrievable` whether a GET resolved (status < 400), `supports_attached_claim` whether
    the source was *positively* confirmed to back the output (honest: `True` only on a
    `supported` judgment), and `flag` the `ok`/`fabricated`/`misattributed` verdict-feed.
    All-string/bool — no `float` field (unlike `Judgment.confidence`), so it canonicalises
    cleanly when the report body is hashed (Story 2.6) with no `clamp_confidence`-style trap.
    """

    source: str
    retrievable: bool
    supports_attached_claim: bool
    flag: CitationFlag


def clamp_confidence(value) -> float:
    """Coerce any raw confidence into a calibrated `float` in `[0.0, 1.0]`.

    A non-finite (`inf`/`nan`) or non-numeric input → `0.0`; `bool` is rejected (it is an
    `int` subclass, so a stray `True`/`False` can't masquerade as `1`/`0`) → `0.0`;
    otherwise `float(min(1.0, max(0.0, value)))`. ALWAYS returns a `float` (never an `int`)
    because confidence is hashed into the report body (Story 2.6) where `0` and `0.0`
    canonicalise to different bytes. Mirrors the `bool`-excluding guard in `max_claims_for_tier`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return float(min(1.0, max(0.0, float(value))))


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

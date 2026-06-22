"""`[B]` Verification Engine — the SDK-agnostic single-pass orchestrator (Story 2.6).

This is the missing `[B]` component from architecture §2: a pure, **SDK-agnostic**
`verify(input, tier) -> Report` that ties the five already-built engine slices together —
claim extraction (`proov.llm.extract_claims`), evidence retrieval
(`proov.search.retrieve_evidence`), per-claim judgment (`proov.llm.judge_claim`), citation
check (`proov.citations.check_citations`) and deterministic aggregation
(`proov.verdict.aggregate_verdict`). It is NOT a re-implementation of any slice: every
slice owns its own clients, timeouts and fallbacks, and the engine only awaits them in the
pipeline order.

Three invariants (architecture §2/§3/§4, NFR2/NFR3):

- **SDK-agnostic.** NO `croo` import — `proov.provider` is the ONLY CROO-coupled module.
  The engine takes a plain validated input `dict` and returns a pure `Report`, so the
  future Deep tier (Story 2.7), the human "try this" path (Story 4.1) and the companion
  caller (Story 4.2) can all invoke verification without the SDK.
- **Single-pass for Quick.** v1 judges claims sequentially (simplest, deterministic; the
  bounded-concurrency / worker-pool is explicitly Story 3.3). Quick = single-pass
  (architecture §4); the multi-pass Deep branch is Story 2.7.
- **Never raises out (degrade, don't drop — NFR3).** A paid order is never crashed by the
  engine. Four of the five slices are already total; only `extract_claims` raises
  (`LLMError`), so only that call is wrapped — an extraction failure degrades to zero
  claims and the rest of the pipeline still runs (→ `partial`/`fail`). `asyncio`
  cancellation is NOT swallowed (it must propagate).

It does NO direct LLM/search/HTTP itself — it only awaits the existing entrypoints.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os

from .citations import check_citations
from .llm import LLMError, extract_claims, get_llm_provider, judge_claim
from .search import retrieve_evidence
from .types import ClaimFinding, Report, Tier
from .verdict import aggregate_verdict

logger = logging.getLogger("proov.engine")

# Per-order SLA budget (NFR2): Quick's wall is 5 min; the default 240s leaves ~60s headroom
# for canonicalise + deliver_order + async settlement. Worker-pool concurrency is Story 3.3.
_DEFAULT_SLA_SECONDS = 240.0


def _resolve_sla_seconds(raw: str | None = None) -> float:
    """Parse `PROOV_QUICK_SLA_SECONDS`, tolerating garbage by falling back to the default.

    Mirrors the `_resolve_timeout` hardening in `search.py`/`llm.py`/`citations.py`: a
    missing / non-numeric / non-finite (`inf`/`nan`) / ≤0 value → `_DEFAULT_SLA_SECONDS`.
    An infinite or zero budget would defeat the per-order SLA bound (NFR2).
    """
    raw = raw if raw is not None else os.environ.get("PROOV_QUICK_SLA_SECONDS")
    if raw is None:
        return _DEFAULT_SLA_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SLA_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_SLA_SECONDS
    return value


def _now() -> float:
    """Monotonic clock for the per-order SLA deadline.

    Indirection (over `asyncio.get_running_loop().time()`) so the early-stop branch is
    deterministically testable by injecting a fake clock — see Story 2.6 AC3 "(or injected
    deadline)". Monotonic and loop-local, so it never goes backwards within a `verify` call.
    """
    return asyncio.get_running_loop().time()


async def verify(input: dict, tier: Tier, *, options: dict | None = None) -> Report:
    """Run the FULL single-pass verification pipeline over a validated input dict.

    `input` is the already-`validate_requirements`-validated PRD §6 object
    (`{output, claims?, sources?, mode?, options?}`) — NOT just the output string. The
    pipeline order is load-bearing (findings/citations are serialised into the hashed report
    body, so extraction order must be preserved for `report_hash` reproducibility):

      (a) `claims = extract_claims(output, …, explicit_claims=input.claims)` — honours the
          PRD §6 explicit-`claims` bypass; an extraction failure degrades to zero claims.
      (b) for each claim in extraction order: `retrieve_evidence` → `judge_claim`,
          accumulating `(claim, judgment)` findings in order — under a per-order SLA budget
          that, when exceeded, stops early and aggregates whatever was judged so far (an
          honest `partial`, NOT a thrown error).
      (c) `citations = check_citations(output, input.sources, …)`.
      (d) `verdict = aggregate_verdict([f.judgment for f in findings], citations, …)`.
      (e) return a `Report(verdict, findings, citations, model)`.

    `opts` merges `input["options"]` with the call-level `options` (call-level wins; both
    absent → `None`). The LLM provider is resolved ONCE and injected into both
    `extract_claims` and `judge_claim` so the stamped `model` is provably the model that
    judged (FR14). Never raises out (NFR3) except for `asyncio.CancelledError`, which
    propagates.
    """
    # Normalise input VALUE types up front so `verify` "never raises out" (NFR3) even for the
    # advertised SDK-agnostic callers (Stories 4.1/4.2) that may bypass `validate_requirements`.
    # The provider path passes a validated dict, but a non-dict `input`, a non-str `output`, a
    # non-dict `options`, or a non-list `claims`/`sources` must degrade gracefully — never raise
    # on `.strip()`, the `{**...}` merge, or by mis-iterating a str/dict as a list.
    if not isinstance(input, dict):
        input = {}
    output_text = input.get("output")
    if not isinstance(output_text, str):
        output_text = ""
    input_opts = input.get("options")
    if not isinstance(input_opts, dict):
        input_opts = None
    explicit_claims = input.get("claims")
    if not isinstance(explicit_claims, list):
        explicit_claims = None
    sources = input.get("sources")
    if not isinstance(sources, list):
        sources = []

    # Merge options: call-level wins over the input's embedded options; both may be absent.
    if input_opts and options:
        opts: dict | None = {**input_opts, **options}
    else:
        opts = options or input_opts or None

    # Per-order SLA deadline (NFR2). Captured once at TRUE entry — BEFORE extraction — so the
    # budget bounds the WHOLE single-pass pipeline (extraction + judging loop), leaving ~60s
    # headroom under Quick's 5-min wall. Checked at the TOP of each judging iteration so we
    # never hard-cancel mid-await (which would lose the in-flight finding and risk a
    # CancelledError escaping). Sequential judging; bounded concurrency is Story 3.3.
    deadline = _now() + _resolve_sla_seconds()

    # Resolve the LLM provider ONCE so the stamped model is the one that judged (FR14).
    # A config error (missing key / unknown provider) must not crash a paid order — degrade
    # to no provider (extraction will then fail → zero claims → honest partial).
    try:
        provider = get_llm_provider()
    except LLMError as exc:
        logger.warning("LLM provider unavailable; verification will degrade: %r", exc)
        provider = None
    model = getattr(provider, "model", "unknown-model")

    # (a) Extraction is the ONE entrypoint that raises out — wrap only this. Degrade an
    # extraction failure to zero claims; the citation+aggregate path still runs (→ partial/
    # fail). A successful-but-empty extraction returns [] too (same downstream path). Do NOT
    # catch CancelledError/BaseException — cancellation must propagate.
    try:
        claims = await extract_claims(
            output_text,
            tier,
            provider=provider,
            options=opts,
            explicit_claims=explicit_claims,
        )
    except LLMError as exc:
        logger.warning(
            "extract_claims failed; degrading to zero claims (→ partial/fail): %r", exc
        )
        claims = []

    # (b) Sequential per-claim judging under the per-order deadline captured above.
    findings: list[ClaimFinding] = []
    for index, claim in enumerate(claims):
        if _now() >= deadline:
            logger.warning(
                "Quick SLA budget hit, stopping after %d/%d claims → partial",
                index,
                len(claims),
            )
            break
        evidence = await retrieve_evidence(claim.text, tier, options=opts)
        judgment = await judge_claim(claim, evidence, tier, provider=provider, options=opts)
        findings.append(ClaimFinding(claim=claim, judgment=judgment))

    # (c) Citation check over the buyer-provided sources (provided-source order preserved).
    citations = await check_citations(output_text, sources, tier, options=opts)

    # (d) Deterministic aggregation (pure/total — consumed unchanged).
    verdict = aggregate_verdict([f.judgment for f in findings], list(citations), options=opts)

    # (e) The SDK-agnostic currency the deliverable builder maps.
    return Report(
        verdict=verdict,
        findings=tuple(findings),
        citations=tuple(citations),
        model=model,
    )

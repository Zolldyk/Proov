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
  The engine takes a plain validated input `dict` and returns a pure `Report`, so both the
  Quick and Deep tiers, the human "try this" path (Story 4.1) and the companion caller
  (Story 4.2) can all invoke verification without the SDK.
- **One orchestration, tier-driven slices.** `verify` runs the SAME ordered single-pass-
  per-claim loop for BOTH tiers (Story 2.7); the tier is the only switch, and the Deep
  differentiators (multi-source retrieval, multi-pass self-consistency judgment,
  provided+discovered citations, the wider SLA budget) live INSIDE the slices / SLA
  resolver, branched on `tier`. The engine still judges claims sequentially (simplest,
  deterministic; bounded-concurrency / worker-pool is explicitly Story 3.3).
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
from .llm import LLMError, _resolve_deep_passes, default_llm_chain, extract_claims, judge_claim
from .metrics import estimate_claim_cost
from .search import retrieve_evidence
from .types import CitationCheck, ClaimFinding, Report, Stance, Tier
from .verdict import aggregate_verdict

logger = logging.getLogger("proov.engine")

# Per-order SLA budget (NFR2), per tier. Quick's wall is 5 min → 240s leaves ~60s headroom;
# Deep's wall is 30 min → 1680s (28 min) leaves ~2 min for canonicalise + (big-report)
# upload_file + deliver_order + async settlement. Worker-pool concurrency is Story 3.3.
_DEFAULT_QUICK_SLA_SECONDS = 240.0
_DEFAULT_DEEP_SLA_SECONDS = 1680.0


def _resolve_sla_seconds(tier: Tier, raw: str | None = None) -> float:
    """Resolve the per-order SLA budget for `tier`, tolerating garbage with the tier default.

    Reads `PROOV_DEEP_SLA_SECONDS` (default `_DEFAULT_DEEP_SLA_SECONDS`) for `"deep"`, else
    `PROOV_QUICK_SLA_SECONDS` (default `_DEFAULT_QUICK_SLA_SECONDS`). Mirrors the
    `_resolve_timeout` hardening in `search.py`/`llm.py`/`citations.py`: a missing /
    non-numeric / non-finite (`inf`/`nan`) / ≤0 value → that tier's default. An infinite or
    zero budget would defeat the per-order SLA bound (NFR2).
    """
    if tier == "deep":
        env_var, default = "PROOV_DEEP_SLA_SECONDS", _DEFAULT_DEEP_SLA_SECONDS
    else:
        env_var, default = "PROOV_QUICK_SLA_SECONDS", _DEFAULT_QUICK_SLA_SECONDS
    raw = raw if raw is not None else os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _resolve_max_order_cost(raw: str | None = None) -> float:
    """Resolve the per-order cost ceiling `PROOV_MAX_ORDER_COST_USD` (Story 3.4, NFR1).

    The spend-twin of `_resolve_sla_seconds`: a missing / non-numeric / non-finite
    (`inf`/`nan`) / negative value → the default **`0.0`**, which is also the **disabled
    sentinel** — a `0.0` ceiling means the engine takes ZERO cost branches, so the default $0
    free-tier path is byte-for-byte unchanged (unlike the SLA resolver, `0.0` is valid here, it
    just disables the meter). Returns a finite float `>= 0`.
    """
    raw = raw if raw is not None else os.environ.get("PROOV_MAX_ORDER_COST_USD")
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
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

    # Per-order SLA deadline (NFR2), per tier. Captured once at TRUE entry — BEFORE extraction
    # — so the budget bounds the WHOLE single-pass pipeline (extraction + judging loop): ~60s
    # headroom under Quick's 5-min wall, ~2 min under Deep's 30-min wall. As of Story 3.3 each
    # slice (retrieve, judge, citation check) is bounded by the REMAINING budget (not just
    # checked at the top of the loop), so a single slow claim — or a Deep claim's ≤7 sequential
    # self-consistency passes — cannot overrun the wall before the next check. Sequential
    # judging; bounded concurrency lives in the provider (Story 3.3 worker-pool).
    deadline = _now() + _resolve_sla_seconds(tier)

    # Resolve the LLM provider CHAIN ONCE (Story 3.4) so a Gemini 429/quota signal routes to the
    # $0 Stub tail instead of dropping the order's claims, and pass the SAME chain into both
    # extraction and judgment. The stamped `model` is the chain HEAD — the configured/advertised
    # primary (e.g. "gemini-2.5-flash"); a quota fallback to the Stub tail is NOT reflected in the
    # single `model` field (Open Question 1, noted in Dev Agent Record). A config error (a forced
    # PROOV_LLM_PROVIDER that's unknown) must not crash a paid order — degrade to an empty chain
    # (→ extraction raises LLMError → zero claims → honest partial; model "unknown-model").
    try:
        chain = default_llm_chain()
    except LLMError as exc:
        logger.warning("LLM provider chain unavailable; verification will degrade: %r", exc)
        chain = []
    model = getattr(chain[0], "model", "unknown-model") if chain else "unknown-model"

    # Per-order cost ceiling (Story 3.4) — the spend-twin of the SLA deadline above. Resolve the
    # ceiling + per-claim marginal cost ONCE; `spent` accumulates as claims are judged. With the
    # default ceiling `0.0` the meter is DISABLED: every `ceiling > 0` branch below is skipped, so
    # the $0 free-tier path is byte-for-byte unchanged (NFR1). When set, the loop stops early →
    # honest `partial` before the projected order cost would breach the ceiling (degrade, don't
    # overspend — NFR3).
    ceiling = _resolve_max_order_cost()
    # A Deep claim is judged by `_resolve_deep_passes()` sequential LLM passes (Story 2.7), so its
    # true marginal cost is the per-pass seam cost × the pass count — metering one pass would
    # undercount Deep spend by ~the pass count and let the ceiling be silently breached. Quick is
    # single-pass (factor 1). Inert at the default 0.0 claim cost.
    claim_cost = estimate_claim_cost(tier)
    if tier == "deep":
        claim_cost *= _resolve_deep_passes()
    spent = 0.0

    # A ceiling smaller than a single claim's marginal cost can never judge even one claim — the
    # loop breaks at index 0 and the order returns an empty `partial` indistinguishable from a real
    # degrade. Surface that misconfiguration once so the operator isn't blind to it.
    if ceiling > 0 and claim_cost > 0 and claim_cost > ceiling:
        logger.warning(
            "%s per-claim cost $%.4f exceeds the per-order ceiling $%.4f — no claim can be judged "
            "within budget; every order will return an empty partial",
            tier,
            claim_cost,
            ceiling,
        )

    # (a) Extraction is the ONE entrypoint that raises out — wrap only this. Degrade an
    # extraction failure to zero claims; the citation+aggregate path still runs (→ partial/
    # fail). A successful-but-empty extraction returns [] too (same downstream path). Do NOT
    # catch CancelledError/BaseException — cancellation must propagate.
    try:
        claims = await extract_claims(
            output_text,
            tier,
            providers=chain,
            options=opts,
            explicit_claims=explicit_claims,
        )
    except LLMError as exc:
        logger.warning(
            "extract_claims failed; degrading to zero claims (→ partial/fail): %r", exc
        )
        claims = []

    # (b) Sequential per-claim judging under the per-order deadline captured above. Each slice
    # is bounded by the REMAINING budget (Story 3.3): a slow `retrieve_evidence` or `judge_claim`
    # cannot push the order past its SLA wall — it trips `asyncio.TimeoutError`, the loop stops
    # early, and the run aggregates whatever was judged → an honest `partial` (NOT a thrown
    # error, NOT a dropped order). `asyncio.wait_for` raises `asyncio.TimeoutError` (a plain
    # `Exception`) on a bound; genuine task cancellation raises `asyncio.CancelledError` (a
    # `BaseException`, a DISTINCT type) which is intentionally NOT caught here and still
    # propagates (NFR3 — cancellation must never be swallowed).
    findings: list[ClaimFinding] = []
    for index, claim in enumerate(claims):
        # Cost ceiling (Story 3.4): the spend-twin of the SLA `remaining` check below. If the
        # next claim's marginal cost would push the order past the ceiling, stop early and
        # aggregate what was judged → honest `partial` (never overspend). Inert when `ceiling`
        # is the default 0.0 (the $0 path takes this branch zero times).
        if ceiling > 0 and spent + claim_cost > ceiling:
            logger.warning(
                "%s cost ceiling $%.4f would be breached, stopping after %d/%d claims "
                "(spent $%.4f) → partial",
                tier,
                ceiling,
                index,
                len(claims),
                spent,
            )
            break
        remaining = deadline - _now()
        if remaining <= 0:
            logger.warning(
                "%s SLA budget hit, stopping after %d/%d claims → partial",
                tier,
                index,
                len(claims),
            )
            break
        try:
            evidence = await asyncio.wait_for(
                retrieve_evidence(claim.text, tier, options=opts), remaining
            )
            remaining = deadline - _now()
            if remaining <= 0:
                logger.warning(
                    "%s SLA budget hit after retrieval, stopping after %d/%d claims → partial",
                    tier,
                    index,
                    len(claims),
                )
                break
            judgment = await asyncio.wait_for(
                judge_claim(claim, evidence, tier, providers=chain, options=opts),
                remaining,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%s SLA budget hit mid-claim, stopping after %d/%d claims → partial",
                tier,
                index,
                len(claims),
            )
            break
        findings.append(ClaimFinding(claim=claim, judgment=judgment))
        # Meter the completed slice (inert at the default 0.0 claim cost).
        spent += claim_cost

    # (c) Citation check over the buyer-provided sources (provided-source order preserved).
    # Deep ALSO covers DISCOVERED sources (architecture §4 "provided + discovered"): the unique
    # evidence-source URLs retrieval surfaced across the run, flagged from the stance the judge
    # already assigned — collected here at zero extra cost (NO re-fetch/re-judge) and passed to
    # `check_citations`. Provided urls are excluded so they are not double-listed. Quick passes
    # nothing (provided-only).
    #
    # Bounded by the REMAINING budget (Story 3.3): a slow/oversized citation fetch on a
    # near-deadline order must not push it past its SLA wall. If the budget is already spent
    # (`remaining <= 0`) the call is skipped; on `asyncio.TimeoutError` it degrades to no
    # citations. Either way the pure/total `aggregate_verdict` still runs → an honest verdict.
    # The cheap discovered-source collection is pure (no await) and runs before the bound.
    # Citation source judging is paid LLM work, so the cost ceiling gates it too (Story 3.4):
    # if the per-order budget is already spent, skip the check → citations degrade to [] (same
    # honest degrade as the SLA-exhausted branch). Inert at the default 0.0 ceiling.
    citations: list[CitationCheck] = []
    remaining = deadline - _now()
    if remaining <= 0:
        logger.warning(
            "%s SLA budget exhausted before citation check → citations degraded to []", tier
        )
    elif ceiling > 0 and spent >= ceiling:
        logger.warning(
            "%s cost ceiling $%.4f reached (spent $%.4f) before citation check → "
            "citations degraded to []",
            tier,
            ceiling,
            spent,
        )
    else:
        # Story 3.4: bound the citation check's paid judge calls by the REMAINING per-order budget.
        # Each retrievable provided source costs up to one `claim_cost` judge call; cap conservatively
        # (count every provided source as paid) so the citation check — the order's largest paid
        # surface — can never push spend past the ceiling. `None` when the meter is disabled (default
        # $0 path) or the per-claim cost is unmetered (0.0), leaving the check unbounded as before.
        max_paid_sources = None
        if ceiling > 0 and claim_cost > 0:
            max_paid_sources = max(0, int((ceiling - spent) / claim_cost))
        try:
            if tier == "deep":
                provided_urls = {
                    src.get("url").strip()
                    for src in sources
                    if isinstance(src, dict)
                    and isinstance(src.get("url"), str)
                    and src["url"].strip()
                }
                discovered: list[tuple[str, Stance]] = []
                seen_sources: set[str] = set()
                for finding in findings:
                    for stance_item in finding.judgment.evidence:
                        source = stance_item.source
                        if source in seen_sources or source in provided_urls:
                            continue
                        seen_sources.add(source)
                        discovered.append((source, stance_item.stance))
                citations = list(
                    await asyncio.wait_for(
                        check_citations(
                            output_text,
                            sources,
                            tier,
                            options=opts,
                            discovered=discovered,
                            max_paid_sources=max_paid_sources,
                        ),
                        remaining,
                    )
                )
            else:
                citations = list(
                    await asyncio.wait_for(
                        check_citations(
                            output_text,
                            sources,
                            tier,
                            options=opts,
                            max_paid_sources=max_paid_sources,
                        ),
                        remaining,
                    )
                )
        except asyncio.TimeoutError:
            logger.warning(
                "%s SLA budget hit at citation check → citations degraded to []", tier
            )
            citations = []

    # (d) Deterministic aggregation (pure/total — consumed unchanged).
    verdict = aggregate_verdict([f.judgment for f in findings], list(citations), options=opts)

    # (e) The SDK-agnostic currency the deliverable builder maps.
    return Report(
        verdict=verdict,
        findings=tuple(findings),
        citations=tuple(citations),
        model=model,
    )

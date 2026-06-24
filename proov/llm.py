"""LLM provider interface + implementations for claim extraction `[C]`.

The verification engine needs an LLM to extract discrete checkable claims (FR6), but the
engine must NOT be coupled to any one vendor — the epic AC is explicit: *"a swap of LLM
provider requires no engine changes (interface honored)."* So everything here hangs off a
structural `LLMProvider` Protocol + a `get_llm_provider` factory; the only code that names
a concrete provider class is the factory.

This module ships:
- `LLMProvider` — `@runtime_checkable` Protocol declaring BOTH `extract_claims` (Story 2.1)
  and `judge_claim` (Story 2.3). Protocols are structural, so a provider qualifies by
  implementing both; the engine depends only on the Protocol + the factory.
- `LLMError` — typed call-failure the engine's Story 1.5 graceful wrapper catches.
- `GeminiProvider` — primary, real async REST calls to Gemini 2.5 Flash via `httpx` with
  structured-JSON output (a STRING array for extraction, an OBJECT for judgment). Key sent
  via `x-goog-api-key` header (never `?key=`), and `register_secret`-ed so it can never leak.
- `StubLLMProvider` — deterministic offline provider (zero network, zero API spend) for
  tests and the `$0` path.
- `_normalise_claims` / `_normalise_judgment` — the single normalisation seams every provider
  routes through (the judgment seam enforces source-grounding + the anti-guess calibration).
- `get_llm_provider` — factory resolving the provider from `PROOV_LLM_PROVIDER`.
- `extract_claims` / `judge_claim` — provider-agnostic top-level entrypoints the engine
  (Story 2.6) calls. Note the deliberate failure-model contrast: `extract_claims` raises
  `LLMError` out (the whole order has nothing to verify), but `judge_claim` degrades a single
  claim to `unverifiable` and NEVER raises out (one claim's failure can't kill an order).

No `croo` import — this is engine `[B]`/`[C]` code, off the SDK-coupled path.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Protocol, runtime_checkable

import httpx

from .redaction import register_secret
from .types import (
    Claim,
    Evidence,
    EvidenceStance,
    Judgment,
    Tier,
    clamp_confidence,
    max_claims_for_tier,
)

logger = logging.getLogger("proov.llm")

# Gemini REST contract (architecture §6/§10, 2026-06-18 stack lock).
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_DEFAULT_MODEL = "gemini-2.5-flash"
_DEFAULT_TIMEOUT = 30.0

# Tight extraction prompt — structured output (a JSON string array) keeps parsing
# deterministic; the cap is reinforced both here and defensively in `_normalise_claims`.
_EXTRACTION_PROMPT = (
    "Extract the discrete, atomic, independently-checkable factual claims stated in the "
    "text below. Each claim must be a single self-contained assertion of fact that could "
    "be verified against an external source. Ignore opinions, instructions, questions, and "
    "filler. Return at most {max_claims} claims as a JSON array of strings, most important "
    "first. If there are no checkable factual claims, return an empty array.\n\nTEXT:\n{text}"
)

# Bounds each judged quote (payload/cost) — judged quotes are short extracts, not the full
# (1000-char-bounded) retrieved snippet. Used by both the Stub and `_normalise_judgment`.
_MAX_QUOTE_CHARS = 300

# Judge prompt — structured OBJECT output. The model is told to cite ONLY from the numbered
# evidence (by its source URL) and to favour precision over recall: return `unverifiable`
# rather than guess when the evidence is insufficient (PRD §1 quality bar / NFR4).
_JUDGMENT_PROMPT = (
    "Judge the following CLAIM against the numbered EVIDENCE. Label it exactly one of "
    "\"supported\", \"unsupported\", or \"unverifiable\". Give a confidence between 0 and 1. "
    "Cite ONLY from the numbered evidence below, by its exact source URL, with a short "
    "verbatim quote and a stance of \"supports\", \"refutes\", or \"neutral\". Do NOT invent "
    "sources. Favour precision over recall: if the evidence is thin, conflicting, or "
    "insufficient to decide, return \"unverifiable\" rather than guessing. Respond as a JSON "
    "object {{\"status\", \"confidence\", \"evidence\": [{{\"source\", \"quote\", \"stance\"}}]}}."
    "\n\nCLAIM:\n{claim}\n\nEVIDENCE:\n{evidence}"
)

# JSON OBJECT schema for the judge's structured output (mirrors the extraction schema's
# uppercase-type convention). The judge returns an object, not extraction's ARRAY of STRING.
_JUDGMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "status": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
        "evidence": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "source": {"type": "STRING"},
                    "quote": {"type": "STRING"},
                    "stance": {"type": "STRING"},
                },
            },
        },
    },
}

# Cosmetic deterministic confidence for the offline Stub judge (any fixed value in [0,1]).
_STUB_JUDGE_CONFIDENCE = 0.5

# Closed coercion sets for `_normalise_judgment` (anything else falls back to the safe value).
_VALID_STATUSES: frozenset[str] = frozenset(("supported", "unsupported", "unverifiable"))
_VALID_STANCES: frozenset[str] = frozenset(("supports", "refutes", "neutral"))

# Deep multi-pass self-consistency (Story 2.7). `PROOV_DEEP_JUDGE_PASSES` (default 3) is how
# many times the Deep `judge_claim` entrypoint samples the provider per claim before reducing
# to one consensus via `_consensus_judgment`; capped at `_MAX_DEEP_PASSES` to bound cost/SLA.
_DEFAULT_DEEP_PASSES = 3
_MAX_DEEP_PASSES = 7
# The consensus merges already-grounded evidence from the winning passes, bounded small.
_MAX_CONSENSUS_EVIDENCE = 6


class LLMError(RuntimeError):
    """A real LLM call failure (transport / HTTP status / timeout / config).

    Raised so the engine's Story 1.5 graceful wrapper can degrade it into an honest
    `unverifiable` deliverable (degrade, don't drop — NFR3). Distinct from a successful
    response that simply yields zero claims, which returns `[]` (a valid empty extraction).
    """


class LLMQuotaError(LLMError):
    """A free-tier rate-limit / quota-exhausted signal (HTTP 429 / `RESOURCE_EXHAUSTED`).

    Raised by `GeminiProvider` SPECIFICALLY on a 429 status so the top-level entrypoints can
    route the call to the next provider in the chain — the LLM twin of search's
    `[Tavily 429]→[Wikipedia]` fall-through (Story 3.4 / NFR1). It subclasses `LLMError`, so
    every existing `except LLMError` (the engine's graceful wrapper, the per-claim degrade)
    still catches it — quota is a *kind of* call failure, just one worth routing past.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Structural interface every LLM provider satisfies.

    Declares BOTH halves of the engine's LLM work: `extract_claims` (Story 2.1) and
    `judge_claim` (Story 2.3). Because this is a `@runtime_checkable` Protocol, conformance
    is proven by a cheap `isinstance` test — so a provider MUST implement both methods to
    qualify, and a future provider drops in with no engine change once it does.
    """

    # The active model id (FR14) — the engine (Story 2.6) reads this off the resolved
    # provider and stamps it into the receipt, so it must be a public attribute on every
    # provider. Gemini → its configured model (e.g. "gemini-2.5-flash"); Stub → "stub-llm".
    model: str

    async def extract_claims(self, text: str, max_claims: int) -> list[Claim]: ...

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment: ...


def _normalise_claims(raw: list[str], max_claims: int) -> list[Claim]:
    """Normalise raw claim strings into capped, deduped, id-assigned `Claim`s.

    Trims whitespace, drops empties, dedupes case-insensitively while preserving
    first-seen order, truncates to `max_claims`, then assigns positional ids `c1`, `c2`, …
    Used by BOTH providers and the explicit-claims path so AC3 holds uniformly everywhere.
    """
    if max_claims <= 0:
        return []
    seen: set[str] = set()
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        claims.append(Claim(id=f"c{len(claims) + 1}", text=text))
        if len(claims) >= max_claims:
            break
    return claims


def _normalise_judgment(raw: dict, evidence: list[Evidence]) -> Judgment:
    """Coerce a raw judge result into a contract-honouring, source-grounded `Judgment`.

    The SINGLE seam every provider (Stub included) routes through, so the precision-over-recall
    guarantee holds uniformly — mirroring how `_normalise_claims` is the one seam for extraction:

    - `status` is coerced into `ClaimStatus`; an unknown/missing value → `"unverifiable"`.
    - `confidence` goes through `clamp_confidence` (always a float in `[0,1]`).
    - **Source-grounding (anti-fabrication):** a judged evidence item is kept ONLY if its
      `source` matches a `source` present in the input `evidence`. A judge-invented source is
      dropped — we never surface a citation we did not actually retrieve. `stance` is coerced
      into `Stance` (unknown → `"neutral"`); `quote` is trimmed, dropped if blank, and bounded
      to `_MAX_QUOTE_CHARS`; an item with a blank `source` is dropped.
    - **Label-needs-evidence (anti-guess):** a `supported`/`unsupported` label with NO surviving
      grounded evidence is downgraded to `"unverifiable"` — we do not assert a confident label
      backed by nothing (PRD §1 "say unverifiable rather than risk a confident wrong verdict").
    """
    allowed_sources = {e.source for e in evidence}

    status = raw.get("status")
    if status not in _VALID_STATUSES:
        status = "unverifiable"

    confidence = clamp_confidence(raw.get("confidence"))

    grounded: list[EvidenceStance] = []
    raw_evidence = raw.get("evidence")
    raw_evidence = raw_evidence if isinstance(raw_evidence, list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if not isinstance(source, str) or source not in allowed_sources:
            continue  # blank or fabricated source → drop (anti-fabrication grounding).
        quote_raw = item.get("quote")
        quote = quote_raw.strip()[:_MAX_QUOTE_CHARS] if isinstance(quote_raw, str) else ""
        if not quote:
            continue
        stance = item.get("stance")
        if stance not in _VALID_STANCES:
            stance = "neutral"
        grounded.append(EvidenceStance(source=source, quote=quote, stance=stance))

    # Calibration guard: a positive/negative label must be backed by ≥1 real evidence item.
    if status in {"supported", "unsupported"} and not grounded:
        status = "unverifiable"

    return Judgment(status=status, confidence=confidence, evidence=tuple(grounded))


class StubLLMProvider:
    """Deterministic, offline `LLMProvider` — zero network, zero API spend.

    The default test / `$0` provider. Extraction is a naive sentence split, so the same
    input always yields the same output. Useful for engine paths and the suite without
    burning Gemini quota (NFR1).
    """

    # Honest model id (FR14): a real engine ran, but NOT Gemini — distinct from the old
    # deliverable placeholder `stub-no-engine` (which meant *no* engine ran at all). The
    # engine stamps this into the receipt for an offline/stub verification.
    model = "stub-llm"

    async def extract_claims(self, text: str, max_claims: int) -> list[Claim]:
        # Split on sentence-ending punctuation; `_normalise_claims` handles trim/dedupe/cap.
        pieces: list[str] = []
        current = []
        for ch in text:
            current.append(ch)
            if ch in ".!?":
                pieces.append("".join(current))
                current = []
        if current:
            pieces.append("".join(current))
        # Strip trailing sentence punctuation so the claim text reads cleanly.
        cleaned = [p.strip().rstrip(".!?").strip() for p in pieces]
        return _normalise_claims(cleaned, max_claims)

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment:
        # No evidence → unverifiable (don't judge with nothing to judge) — same honest
        # outcome the real provider and the top-level entrypoint produce for thin evidence.
        if not evidence:
            return Judgment("unverifiable", 0.0, ())
        # Deterministic offline verdict: every piece of evidence "supports" with a fixed
        # confidence. Route through `_normalise_judgment` so the Stub honours the SAME
        # grounding/calibration guards as the real provider (one code path).
        raw = {
            "status": "supported",
            "confidence": _STUB_JUDGE_CONFIDENCE,
            "evidence": [
                {
                    "source": e.source,
                    "quote": e.snippet[:_MAX_QUOTE_CHARS],
                    "stance": "supports",
                }
                for e in evidence
            ],
        }
        return _normalise_judgment(raw, evidence)


class GeminiProvider:
    """Primary `LLMProvider` — Gemini 2.5 Flash via raw async REST (httpx, structured JSON).

    A thin raw-REST provider (no vendor SDK) keeps the dependency surface minimal and the
    interface swappable. The `httpx.AsyncClient` is injectable for offline tests, mirroring
    `ProviderAdapter(cfg, client=…)`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        # Public per FR14: the engine (Story 2.6) reads `.model` off the resolved provider
        # and stamps it into the receipt. `_model` stays as the internal name the REST calls
        # use; `model` is the public contract the `LLMProvider` Protocol declares.
        self._model = model
        self.model = model
        self._timeout = timeout
        self._client = client
        # The Gemini key is NOT `croo_sk_`-shaped, so the standing redaction filter won't
        # scrub it unless registered. Register it so it can never leak into a log.
        register_secret(api_key)

    async def extract_claims(self, text: str, max_claims: int) -> list[Claim]:
        if not text.strip():
            return []  # nothing to extract — skip the call (and the spend) entirely.

        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": _EXTRACTION_PROMPT.format(
                                max_claims=max_claims, text=text
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        # Key in the header, NEVER the URL — keeps it out of any logged request line.
        url = _GEMINI_ENDPOINT.format(model=self._model)
        headers = {"x-goog-api-key": self._api_key}

        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Transport / status / timeout — a real failure. Raise typed so the engine's
            # graceful seam degrades it to `unverifiable` (degrade, don't drop — NFR3).
            # Do NOT let an outage masquerade as a confident empty extraction.
            # A 429 is a quota/rate-limit signal → raise the routing subclass so the chain
            # falls through to the next provider (the $0 Stub tail). Only an `HTTPStatusError`
            # carries `.response`; a transport/timeout error has none → stays a plain LLMError.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                raise LLMQuotaError("Gemini extract_claims hit a 429 quota/rate limit") from exc
            raise LLMError(f"Gemini extract_claims call failed: {exc!r}") from exc

        # A successful 200 whose body is unparseable / candidate-less / claim-less is a
        # VALID empty extraction, not a failure — log a warning and return [] (AC6).
        try:
            payload = response.json()
            candidate_text = payload["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(candidate_text)
            if not isinstance(parsed, list):
                raise ValueError("candidate text is not a JSON array")
        except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Gemini returned an unparseable/empty extraction: %r", exc)
            return []
        return _normalise_claims(parsed, max_claims)

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment:
        # Empty evidence short-circuits to unverifiable with NO network call — don't judge
        # (and don't spend) with nothing to judge.
        if not evidence:
            return Judgment("unverifiable", 0.0, ())

        numbered = "\n".join(
            f"{i}. [{e.source}] {e.snippet}" for i, e in enumerate(evidence, start=1)
        )
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": _JUDGMENT_PROMPT.format(
                                claim=claim.text, evidence=numbered
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _JUDGMENT_SCHEMA,
            },
        }
        # Key in the header, NEVER the URL — same as extract_claims; reuses the same key.
        url = _GEMINI_ENDPOINT.format(model=self._model)
        headers = {"x-goog-api-key": self._api_key}

        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Transport / status / timeout — a real failure. Raise typed so the top-level
            # `judge_claim` entrypoint can log the real reason and degrade this ONE claim to
            # unverifiable. Do NOT let an outage masquerade as a confident unverifiable here.
            # A 429 is a quota/rate-limit signal → raise the routing subclass so the chain
            # falls through to the next provider per-pass. Guard `.response` on the status type.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                raise LLMQuotaError("Gemini judge_claim hit a 429 quota/rate limit") from exc
            raise LLMError(f"Gemini judge_claim call failed: {exc!r}") from exc

        # A successful 200 whose body is unparseable / shape-less is the VALID-empty judgment
        # `unverifiable` (a logged warning, not an exception) — the same LLMError-vs-empty
        # split `extract_claims` uses, with `unverifiable` playing the role `[]` plays there.
        try:
            payload = response.json()
            candidate_text = payload["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(candidate_text)
            if not isinstance(parsed, dict):
                raise ValueError("candidate text is not a JSON object")
        except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Gemini returned an unparseable/shape-less judgment: %r", exc)
            return Judgment("unverifiable", 0.0, ())
        return _normalise_judgment(parsed, evidence)


def get_llm_provider(name: str | None = None) -> LLMProvider:
    """Resolve an `LLMProvider` by name (the only place concrete classes are named).

    `name` (or `PROOV_LLM_PROVIDER`, default `"gemini"`): `"stub"` → `StubLLMProvider`;
    `"gemini"` → `GeminiProvider` configured from env (`PROOV_LLM_MODEL`,
    `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `PROOV_LLM_TIMEOUT`). A missing key or unknown name
    raises `LLMError` (naming the missing var, never echoing a value — NFR5).
    """
    resolved = (name or os.environ.get("PROOV_LLM_PROVIDER") or "gemini").strip().lower()

    if resolved == "stub":
        return StubLLMProvider()
    if resolved == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise LLMError(
                "Gemini provider requires GEMINI_API_KEY (or GOOGLE_API_KEY) to be set."
            )
        model = os.environ.get("PROOV_LLM_MODEL") or _DEFAULT_MODEL
        timeout = _resolve_timeout(os.environ.get("PROOV_LLM_TIMEOUT"))
        return GeminiProvider(api_key=api_key, model=model, timeout=timeout)
    raise LLMError(f"Unknown LLM provider: {resolved!r}")


def default_llm_chain() -> list[LLMProvider]:
    """Build the ordered LLM provider chain — the quota-aware fallback (Story 3.4, NFR1).

    The structural twin of `search.default_search_chain` (the module already calls itself
    "the structural twin of `proov/search.py`" — this closes the one feature it was missing):

    - If `PROOV_LLM_PROVIDER` is set, return that SINGLE forced provider (via
      `get_llm_provider()`) — an operator override is honoured verbatim, no surprise tail.
    - Otherwise build `[GeminiProvider] if a key is present else []` **followed by** the
      always-available keyless offline `StubLLMProvider` `$0` tail — Gemini-first-when-keyed,
      always ending in the no-key provider (the LLM analogue of keyless Wikipedia ending the
      search chain), so the chain is **never empty** and a Gemini 429 routes to a free $0
      verification instead of dropping the order's claims (degrade, don't drop — NFR3).

    Cerebras/Groq/Ollama (architecture §6) are documented pluggable slots: a future story adds
    a provider class + an entry here, with NO engine change (the whole point of the Protocol).
    """
    if os.environ.get("PROOV_LLM_PROVIDER"):
        return [get_llm_provider()]

    chain: list[LLMProvider] = []
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        model = os.environ.get("PROOV_LLM_MODEL") or _DEFAULT_MODEL
        timeout = _resolve_timeout(os.environ.get("PROOV_LLM_TIMEOUT"))
        chain.append(GeminiProvider(api_key=api_key, model=model, timeout=timeout))
    chain.append(StubLLMProvider())
    return chain


def _resolve_timeout(raw: str | None) -> float:
    """Parse `PROOV_LLM_TIMEOUT`, tolerating garbage by falling back to the default."""
    if raw is None:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    # Reject non-finite (inf/nan) as well as ≤0 — an infinite timeout is "no timeout",
    # which would let a hung Gemini connection block a paid order past its SLA.
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_TIMEOUT
    return value


def _resolve_chain(
    providers: list[LLMProvider] | None, provider: LLMProvider | None
) -> list[LLMProvider]:
    """Resolve the active LLM provider chain for an entrypoint call (Story 3.4).

    Precedence: an explicit `providers` chain (what the engine passes — `default_llm_chain()`
    resolved once) wins; else a single back-compat `provider` becomes a one-element chain (so
    the existing `provider=`-injecting tests are untouched); else build `default_llm_chain()`.
    """
    if providers is not None:
        return providers
    if provider is not None:
        return [provider]
    return default_llm_chain()


async def extract_claims(
    text: str,
    tier: Tier,
    *,
    provider: LLMProvider | None = None,
    providers: list[LLMProvider] | None = None,
    options: dict | None = None,
    explicit_claims: list[str] | None = None,
) -> list[Claim]:
    """Provider-agnostic claim-extraction entrypoint (the engine, Story 2.6, calls this).

    Resolves the tier cap (lowerable via `options.max_claims`), then: if `explicit_claims`
    is supplied and non-empty, uses them verbatim (normalised + capped) with NO LLM call
    (PRD §6 explicit-`claims` bypass, AC4); otherwise routes through the **provider chain**
    (Story 3.4): each provider is tried in order and an `LLMError` (quota OR generic) routes to
    the next, so a Gemini 429 falls through to the `$0` Stub tail rather than dropping the
    order's claims. A successful (incl. validly-empty `[]`) extraction is returned immediately
    — only a *failure* falls through. If the whole chain fails (or is empty) the last
    `LLMError` is re-raised so the engine's graceful wrapper degrades it to zero claims.
    """
    cap = max_claims_for_tier(tier, options)
    if explicit_claims:
        # Explicit claims replace extraction (AC4) — but only if at least one survives
        # normalisation. An all-blank list normalises to [] and must NOT silently swallow
        # the extraction: fall through to the provider instead.
        normalised = _normalise_claims(explicit_claims, cap)
        if normalised:
            return normalised
    chain = _resolve_chain(providers, provider)
    last_error: LLMError | None = None
    for active in chain:
        try:
            return await active.extract_claims(text, cap)
        except LLMError as exc:
            last_error = exc
            logger.warning("extract_claims provider failed, routing to next provider: %r", exc)
            continue
    # Chain exhausted (or empty): re-raise so the engine degrades to zero claims (→ partial).
    raise last_error if last_error is not None else LLMError(
        "extract_claims has no available LLM provider"
    )


def _resolve_deep_passes(raw: str | None = None) -> int:
    """Resolve `PROOV_DEEP_JUDGE_PASSES`, tolerating garbage by falling back to the default.

    Mirrors the `_resolve_timeout` hardening shape: a missing / non-int / ≤0 value →
    `_DEFAULT_DEEP_PASSES`; a valid value is capped at `_MAX_DEEP_PASSES` so a misconfigured
    huge count can't blow the Deep cost/SLA budget. Returns a positive `int`.
    """
    raw = raw if raw is not None else os.environ.get("PROOV_DEEP_JUDGE_PASSES")
    if raw is None:
        return _DEFAULT_DEEP_PASSES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DEEP_PASSES
    if value <= 0:
        return _DEFAULT_DEEP_PASSES
    return min(value, _MAX_DEEP_PASSES)


def _consensus_judgment(judgments: list[Judgment]) -> Judgment:
    """Reduce N self-consistency passes to ONE consensus `Judgment` (pure, deterministic).

    The robustness mechanism behind Deep multi-pass (Story 2.7 / architecture §4). No I/O, no
    provider call — every input was already produced by `_normalise_judgment`, so every quote
    is already grounded/bounded (no new fabrication surface). Order-independent so a shuffled
    set of passes yields an identical consensus (same canonicalisation discipline as
    `aggregate_verdict`, Story 2.5 review):

    - **Status — majority wins.** The status with the strictly-highest vote count is the label.
      A tie at the top count (incl. an all-distinct split, e.g. 1/1/1) → `"unverifiable"`
      (precision over recall, NFR4 — never a coin-flip verdict). A pass degraded to
      `unverifiable` (thin evidence / `LLMError`) counts as an `unverifiable` vote.
    - **Confidence** = `clamp_confidence(mean(confidence of the winning-status passes))`
      down-weighted by agreement `(winning_votes / total_passes)` — unanimity keeps the full
      mean, a bare majority is penalised. `math.fsum` over a sorted list → order-independent.
    - **Evidence** = the grounded stances from the winning passes, deduped by
      `(source, quote, stance)` and deterministically ordered (sorted, so the result is
      invariant to pass order), bounded to `_MAX_CONSENSUS_EVIDENCE`.
    """
    if not judgments:
        return Judgment("unverifiable", 0.0, ())
    total = len(judgments)

    counts: dict[str, int] = {}
    for j in judgments:
        counts[j.status] = counts.get(j.status, 0) + 1
    top = max(counts.values())
    winners = [status for status, count in counts.items() if count == top]
    if len(winners) != 1:
        # No unique plurality (tie) → unverifiable. Honest "we couldn't agree" outcome.
        return Judgment("unverifiable", 0.0, ())
    status = winners[0]

    winning = [j for j in judgments if j.status == status]
    mean_conf = math.fsum(sorted(j.confidence for j in winning)) / len(winning)
    confidence = clamp_confidence(mean_conf * (len(winning) / total))

    unique = {
        (es.source, es.quote, es.stance) for j in winning for es in j.evidence
    }
    evidence = tuple(
        EvidenceStance(source=source, quote=quote, stance=stance)
        for source, quote, stance in sorted(unique)
    )[:_MAX_CONSENSUS_EVIDENCE]
    return Judgment(status, confidence, evidence)


async def _judge_one_pass(
    claim: Claim, evidence: list[Evidence], chain: list[LLMProvider]
) -> Judgment:
    """One judgment pass over the provider chain — first provider that answers wins (Story 3.4).

    Walks `chain` in order; a provider that raises `LLMError` (incl. `LLMQuotaError`) routes to
    the next provider for this pass. If every provider in the chain fails (or the chain is
    empty), the pass degrades to an `unverifiable` vote — so the entrypoint never raises out
    (degrade, don't drop — NFR3). Shared by the Quick single-pass and each Deep pass.
    """
    for active in chain:
        try:
            return await active.judge_claim(claim, list(evidence))
        except LLMError as exc:
            logger.warning("judge_claim provider failed, routing to next provider: %r", exc)
            continue
    return Judgment("unverifiable", 0.0, ())


async def judge_claim(
    claim: Claim,
    evidence: list[Evidence],
    tier: Tier,
    *,
    provider: LLMProvider | None = None,
    providers: list[LLMProvider] | None = None,
    options: dict | None = None,
) -> Judgment:
    """Provider-agnostic per-claim judgment entrypoint (the engine, Story 2.6, calls this).

    Deliberately UNLIKE `extract_claims`, which raises `LLMError` out: judgment is per-claim,
    and the engine judges many claims in a loop, so one claim's judge failure must NOT nuke a
    paid multi-claim order. Thin evidence and a failed call therefore BOTH resolve to the same
    honest `unverifiable` — this entrypoint **never raises out** (degrade, don't drop — NFR3):

    - empty `evidence` → `Judgment("unverifiable", 0.0, ())` with NO provider call (no
      multi-pass spend on nothing);
    - `tier == "quick"`: **single-pass** over the provider chain (Story 3.4) — the first
      provider that answers wins; a provider raising `LLMError`/`LLMQuotaError` routes to the
      next (the `$0` Stub tail), and a fully-exhausted chain degrades THIS claim to
      `unverifiable`.
    - `tier == "deep"`: **multi-pass self-consistency** (Story 2.7 / architecture §4) — run
      `_resolve_deep_passes()` passes, each itself a chain walk (`_judge_one_pass`): a pass
      whose head provider 429s falls through to the next provider FOR THAT PASS; a fully-failed
      pass becomes an `unverifiable` vote. Reduce to one consensus via `_consensus_judgment`.
      The provider classes and `_normalise_judgment` are unchanged — multi-pass + chain routing
      are orchestration here, not new provider logic.
    """
    if not evidence:
        return Judgment("unverifiable", 0.0, ())
    chain = _resolve_chain(providers, provider)

    if tier == "deep":
        votes = [
            await _judge_one_pass(claim, evidence, chain)
            for _ in range(_resolve_deep_passes())
        ]
        return _consensus_judgment(votes)

    return await _judge_one_pass(claim, evidence, chain)

"""LLM provider interface + implementations for claim extraction `[C]`.

The verification engine needs an LLM to extract discrete checkable claims (FR6), but the
engine must NOT be coupled to any one vendor — the epic AC is explicit: *"a swap of LLM
provider requires no engine changes (interface honored)."* So everything here hangs off a
structural `LLMProvider` Protocol + a `get_llm_provider` factory; the only code that names
a concrete provider class is the factory.

Story 2.1 ships:
- `LLMProvider` — `@runtime_checkable` Protocol declaring ONLY `extract_claims`
  (`judge_claim` is Story 2.3; Protocols are structural so 2.3 can extend without breaking
  this).
- `LLMError` — typed call-failure the engine's Story 1.5 graceful wrapper catches.
- `GeminiProvider` — primary, real async REST call to Gemini 2.5 Flash via `httpx` with
  structured-JSON output. Key sent via `x-goog-api-key` header (never `?key=`), and
  `register_secret`-ed so it can never leak into a log.
- `StubLLMProvider` — deterministic offline provider (zero network, zero API spend) for
  tests and the `$0` path.
- `get_llm_provider` — factory resolving the provider from `PROOV_LLM_PROVIDER`.
- `extract_claims` — provider-agnostic top-level entrypoint the engine (Story 2.6) calls;
  honours the PRD §6 explicit-`claims` bypass and the tier caps.

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
from .types import Claim, Tier, max_claims_for_tier

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


class LLMError(RuntimeError):
    """A real LLM call failure (transport / HTTP status / timeout / config).

    Raised so the engine's Story 1.5 graceful wrapper can degrade it into an honest
    `unverifiable` deliverable (degrade, don't drop — NFR3). Distinct from a successful
    response that simply yields zero claims, which returns `[]` (a valid empty extraction).
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Structural interface every LLM provider satisfies.

    Declares ONLY `extract_claims` for now. `judge_claim` is Story 2.3 — do not add it
    here. Because this is a `@runtime_checkable` Protocol, conformance is proven by a cheap
    `isinstance` test and a future provider need only implement this method.
    """

    async def extract_claims(self, text: str, max_claims: int) -> list[Claim]: ...


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


class StubLLMProvider:
    """Deterministic, offline `LLMProvider` — zero network, zero API spend.

    The default test / `$0` provider. Extraction is a naive sentence split, so the same
    input always yields the same output. Useful for engine paths and the suite without
    burning Gemini quota (NFR1).
    """

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
        self._model = model
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


async def extract_claims(
    text: str,
    tier: Tier,
    *,
    provider: LLMProvider | None = None,
    options: dict | None = None,
    explicit_claims: list[str] | None = None,
) -> list[Claim]:
    """Provider-agnostic claim-extraction entrypoint (the engine, Story 2.6, calls this).

    Resolves the tier cap (lowerable via `options.max_claims`), then: if `explicit_claims`
    is supplied and non-empty, uses them verbatim (normalised + capped) with NO LLM call
    (PRD §6 explicit-`claims` bypass, AC4); otherwise delegates to `provider` (or the
    factory default) — which honours `max_claims` defensively too.
    """
    cap = max_claims_for_tier(tier, options)
    if explicit_claims:
        # Explicit claims replace extraction (AC4) — but only if at least one survives
        # normalisation. An all-blank list normalises to [] and must NOT silently swallow
        # the extraction: fall through to the provider instead.
        normalised = _normalise_claims(explicit_claims, cap)
        if normalised:
            return normalised
    active = provider if provider is not None else get_llm_provider()
    return await active.extract_claims(text, cap)

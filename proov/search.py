"""Search provider interface + implementations for evidence retrieval `[D]` (RAG, FR7).

The verification engine needs real, source-linked evidence for each extracted claim, but
the engine must NOT be coupled to any one search backend — architecture §2/§7 demands the
search backend be swappable behind one interface, and the epic AC is explicit: it *"falls
back to a secondary source on failure."* So everything here hangs off a structural
`SearchProvider` Protocol + a `get_search_provider` factory / `default_search_chain`; the
only code that names a concrete provider class is the factory and the chain builder.

This module is the structural twin of `proov/llm.py` — it follows the same beats
(Protocol → `*Error` → `_normalise_*` → Stub → real providers → factory → top-level
entrypoint) and reuses the same hardened `_resolve_timeout` pattern.

Story 2.2 ships:
- `SearchProvider` — `@runtime_checkable` Protocol declaring ONLY `search`.
- `SearchError` — typed call-failure the fallback chain catches to fall through.
- `WikipediaProvider` — keyless primary/fallback, real async REST to the MediaWiki REST
  `search/page` endpoint via `httpx`.
- `TavilyProvider` — key-gated RAG-native source, real async `POST /search` via `httpx`,
  key sent via `Authorization: Bearer` header (never URL/body) and `register_secret`-ed.
- `StubSearchProvider` — deterministic offline provider (zero network, zero API spend).
- `get_search_provider` / `default_search_chain` — factory + ordered chain builder.
- `retrieve_evidence` — provider-agnostic top-level entrypoint that applies a per-call
  timeout and falls back across providers, returning `[]` (never raising) if all fail.

**The retrieval failure model differs from extraction on purpose** (FR7 mandates fallback):
a single provider's `search()` raises `SearchError` on a real call failure, but the
top-level `retrieve_evidence` catches it and tries the next provider — and if every
provider fails/times out, it returns `[]` (the claim becomes `unverifiable` downstream, a
valid graceful outcome — NFR3). `retrieve_evidence` never raises.

No `croo` import — this is engine `[B]`/`[D]` code, off the SDK-coupled path.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from typing import Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from .cache import EvidenceCache, evidence_cache_key, get_evidence_cache
from .redaction import register_secret
from .types import Evidence, Tier, evidence_k_for_tier

logger = logging.getLogger("proov.search")

# Search API contracts (architecture §6/§10 decision 3, web-verified 2026-06).
_WIKIPEDIA_ENDPOINT = "https://en.wikipedia.org/w/rest.php/v1/search/page"
_WIKIPEDIA_ARTICLE_URL = "https://en.wikipedia.org/wiki/{key}"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_DEFAULT_SEARCH_TIMEOUT = 10.0

# Each snippet is bounded so a single result can't bloat the payload / LLM-judge cost.
_MAX_SNIPPET_CHARS = 1000

# Strip MediaWiki excerpt highlight markup (e.g. `<span class="searchmatch">…</span>`).
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class SearchError(RuntimeError):
    """A real search call failure (transport / HTTP status / timeout / config).

    Raised by a single provider's `search()` so the top-level `retrieve_evidence` fallback
    chain can catch it and fall through to the next provider (FR7). Distinct from a
    successful response that simply yields zero results, which returns `[]` from the
    provider (a valid empty retrieval).
    """


@runtime_checkable
class SearchProvider(Protocol):
    """Structural interface every search provider satisfies.

    Declares ONLY `search` (exactly the architecture §7 signature). Because this is a
    `@runtime_checkable` Protocol, conformance is proven by a cheap `isinstance` test and a
    future backend (e.g. Serper) need only implement this method and register in the factory
    — no engine change (the load-bearing pluggability AC).
    """

    async def search(self, query: str, k: int) -> list[Evidence]: ...


def _strip_html(text: str) -> str:
    """Strip HTML tags (MediaWiki excerpt markup) and collapse whitespace to clean text."""
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub("", text)).strip()


def _normalise_evidence(raw: list[Evidence], k: int) -> list[Evidence]:
    """Normalise raw `Evidence` into a deduped, snippet-bounded, capped list.

    Drops entries with a blank `source` or blank `snippet`, dedupes by `source` URL
    (first-seen wins, preserving order), truncates each `snippet` to `_MAX_SNIPPET_CHARS`,
    and caps the list to `k`. Used by every provider AND the cross-provider merge step so
    the pluggability/normalisation guarantee (AC6) holds uniformly. Mirrors
    `proov.llm._normalise_claims`.
    """
    if k <= 0:
        return []
    seen: set[str] = set()
    out: list[Evidence] = []
    for item in raw:
        if not isinstance(item, Evidence):
            continue
        source = item.source.strip()
        snippet = item.snippet.strip()
        if not source or not snippet:
            continue
        if source in seen:
            continue
        seen.add(source)
        out.append(
            Evidence(
                source=source,
                title=item.title.strip(),
                snippet=snippet[:_MAX_SNIPPET_CHARS],
                score=item.score,
            )
        )
        if len(out) >= k:
            break
    return out


def _slug(text: str) -> str:
    """A deterministic URL-safe slug from arbitrary text (for the offline stub)."""
    cleaned = _WHITESPACE_RE.sub("-", text.strip().lower())
    return re.sub(r"[^a-z0-9\-]", "", cleaned) or "evidence"


class StubSearchProvider:
    """Deterministic, offline `SearchProvider` — zero network, zero API spend.

    The default test / `$0` provider. Derives canned `Evidence` from the query, so the same
    input always yields the same output. Lets the engine and the suite run with no network
    and no API spend (NFR1).
    """

    async def search(self, query: str, k: int) -> list[Evidence]:
        if not query.strip() or k <= 0:
            return []
        slug = _slug(query)
        # Derive a small, deterministic set of distinct evidences from the query.
        raw = [
            Evidence(
                source=f"https://stub.local/{slug}/{i}",
                title=query[:60],
                snippet=f"Stub evidence #{i} for: {query}",
            )
            for i in range(1, k + 1)
        ]
        return _normalise_evidence(raw, k)


class WikipediaProvider:
    """Keyless primary/fallback `SearchProvider` — MediaWiki REST `search/page` via httpx.

    Wikipedia needs no key, so it is the always-available fallback that the default chain
    always ends in. The `httpx.AsyncClient` is injectable for offline tests, mirroring
    `GeminiProvider(client=…)`.
    """

    def __init__(self, *, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._timeout = timeout
        self._client = client

    async def search(self, query: str, k: int) -> list[Evidence]:
        if not query.strip():
            return []  # nothing to search — skip the call entirely.

        params = {"q": query, "limit": k}
        try:
            if self._client is not None:
                response = await self._client.get(_WIKIPEDIA_ENDPOINT, params=params)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(_WIKIPEDIA_ENDPOINT, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Transport / status / timeout — a real failure. Raise typed so the chain in
            # `retrieve_evidence` falls through to the next provider (FR7). Do NOT let an
            # outage masquerade as a confident empty result.
            raise SearchError(f"Wikipedia search call failed: {exc!r}") from exc

        # A successful 200 whose body is unparseable / shape-less is a VALID empty result,
        # not a failure — log a warning and return [] (AC5).
        try:
            pages = response.json()["pages"]
            # Skip a page missing its `key` rather than letting one bad entry drop the whole
            # batch; URL-encode the key so titles with spaces/special chars yield a valid link
            # (leave `/` for subpage titles, which Wikipedia serves literally).
            raw = [
                Evidence(
                    source=_WIKIPEDIA_ARTICLE_URL.format(key=quote(p["key"], safe="/")),
                    title=p.get("title", ""),
                    snippet=_strip_html(p.get("excerpt") or p.get("description") or ""),
                )
                for p in pages
                if p.get("key")
            ]
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            logger.warning("Wikipedia returned an unparseable/empty result: %r", exc)
            return []
        return _normalise_evidence(raw, k)


class TavilyProvider:
    """Key-gated RAG-native `SearchProvider` — Tavily `POST /search` via httpx.

    `content` is a citation-ready extract (why Tavily is "RAG-native"). The key is sent in
    the `Authorization: Bearer` **header** (never the URL/body), and `register_secret`-ed so
    it can never leak into a log (a `tvly-` key is not `croo_sk_`-shaped, so the standing
    redaction filter won't scrub it otherwise — NFR5). Client injectable for offline tests.
    """

    def __init__(
        self,
        *,
        api_key: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._client = client
        register_secret(api_key)

    async def search(self, query: str, k: int) -> list[Evidence]:
        if not query.strip():
            return []  # nothing to search — skip the call (and the spend) entirely.

        body = {"query": query, "max_results": k, "search_depth": "basic"}
        # Key in the header, NEVER the URL/body — keeps it out of any logged request line.
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = await self._client.post(
                    _TAVILY_ENDPOINT, json=body, headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        _TAVILY_ENDPOINT, json=body, headers=headers
                    )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchError(f"Tavily search call failed: {exc!r}") from exc

        try:
            results = response.json()["results"]
            # Skip a result missing its `url` rather than dropping the whole batch on one
            # malformed entry.
            raw = [
                Evidence(
                    source=r["url"],
                    title=r.get("title", ""),
                    snippet=r.get("content", ""),
                    score=r.get("score"),
                )
                for r in results
                if r.get("url")
            ]
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            logger.warning("Tavily returned an unparseable/empty result: %r", exc)
            return []
        return _normalise_evidence(raw, k)


def _resolve_timeout(raw: str | None) -> float:
    """Parse `PROOV_SEARCH_TIMEOUT`, tolerating garbage by falling back to the default.

    Hardened identically to `proov.llm._resolve_timeout`: rejects non-finite (inf/nan) as
    well as ≤0 — an infinite timeout is "no timeout", which would let a hung search
    connection block a paid order past its SLA (NFR2).
    """
    if raw is None:
        return _DEFAULT_SEARCH_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SEARCH_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_SEARCH_TIMEOUT
    return value


def get_search_provider(name: str | None = None) -> SearchProvider:
    """Resolve a single `SearchProvider` by name (one of two places concrete classes appear).

    `name` (or `PROOV_SEARCH_PROVIDER`): `"wikipedia"` → `WikipediaProvider`; `"stub"` →
    `StubSearchProvider`; `"tavily"` → `TavilyProvider` configured from `TAVILY_API_KEY`. A
    missing key or unknown name raises `SearchError` (naming the missing var, never echoing
    a value — NFR5). There is intentionally NO hard default here — `default_search_chain`
    owns the auto Tavily→Wikipedia chaining when no provider is forced.
    """
    resolved = (name or os.environ.get("PROOV_SEARCH_PROVIDER") or "").strip().lower()
    timeout = _resolve_timeout(os.environ.get("PROOV_SEARCH_TIMEOUT"))

    if resolved == "stub":
        return StubSearchProvider()
    if resolved == "wikipedia":
        return WikipediaProvider(timeout=timeout)
    if resolved == "tavily":
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise SearchError("Tavily provider requires TAVILY_API_KEY to be set.")
        return TavilyProvider(api_key=api_key, timeout=timeout)
    raise SearchError(f"Unknown search provider: {resolved!r}")


def default_search_chain() -> list[SearchProvider]:
    """Build the ordered provider chain (the other place concrete classes appear).

    If `PROOV_SEARCH_PROVIDER` is set, return a single forced provider. Otherwise build
    `[Tavily] if TAVILY_API_KEY else []` **followed by** `[Wikipedia]` — Tavily-first-when-
    keyed, always ending in the keyless Wikipedia fallback (AC6), so the chain is never
    empty and always has a no-key source.
    """
    if os.environ.get("PROOV_SEARCH_PROVIDER"):
        return [get_search_provider()]

    timeout = _resolve_timeout(os.environ.get("PROOV_SEARCH_TIMEOUT"))
    chain: list[SearchProvider] = []
    api_key = os.environ.get("TAVILY_API_KEY")
    if api_key:
        chain.append(TavilyProvider(api_key=api_key, timeout=timeout))
    chain.append(WikipediaProvider(timeout=timeout))
    return chain


async def retrieve_evidence(
    query: str,
    tier: Tier,
    *,
    providers: list[SearchProvider] | None = None,
    options: dict | None = None,
    timeout: float | None = None,
    cache: EvidenceCache | None = None,
) -> list[Evidence]:
    """Provider-agnostic evidence-retrieval entrypoint (the engine, Story 2.6, calls this).

    Resolves the per-tier `k` (lowerable via `options`) and a per-call timeout, then tries
    each provider **in order under `asyncio.wait_for`**. On a provider raising `SearchError`
    OR timing out, it logs a warning and falls through to the next provider (FR7 fallback).

    **Claim→evidence cache (Story 2.8, FR11).** Before building the chain, the
    `(query, tier, k)`-keyed cache is consulted: a HIT returns the stored evidence with **zero**
    search-provider calls. On a miss the chain runs and a **non-empty** result is stored (an
    empty result is NOT cached — caching `[]` would pin a transient outage as "no evidence" for
    the whole TTL). The cache is **best-effort**: every cache failure degrades to a miss/no-op,
    so it never changes the never-raises-out contract or the data the live path would return —
    it only changes timing/cost. Inject `cache=` (e.g. a `:memory:` `SqliteEvidenceCache`) to
    override the memoised default (the suite injects / disables it).

    Chain-stop policy is **tier-keyed** (architecture §4):

    - `tier == "quick"`: **stop at the first provider that returns ≥1 evidence** (cheapest;
      "≈1 source/claim"), falling through only on failure / timeout / empty.
    - `tier == "deep"`: **multi-source merge** — query EVERY provider in the chain, accumulate
      their results, and return `_normalise_evidence(combined, k)` (dedupe-by-source first-seen,
      snippet-bounded, capped to the Deep `k`). A failed/timed-out provider simply contributes
      nothing to the merge rather than aborting it.

    Either way each provider is called under the per-call `asyncio.wait_for` timeout, and a
    `SearchError`/timeout/unexpected exception logs a warning and falls through to the next
    (FR7). If every provider fails / times out / yields nothing, returns `[]` — a valid "no
    evidence" outcome that makes the claim `unverifiable` downstream (degrade, don't drop —
    NFR3). This function NEVER raises out.
    """
    if not query.strip():
        return []

    k = evidence_k_for_tier(tier, options)
    # Cache-first (Story 2.8): a hit short-circuits the whole provider chain — zero search
    # calls. The cache is best-effort: the built-in `SqliteEvidenceCache` already swallows its
    # own failures, but the guard here also protects the never-raises-out contract from a
    # MISBEHAVING injected cache (`asyncio.CancelledError` is a BaseException — not caught here,
    # so cancellation still propagates). A failure → treat as a miss and run the live search.
    active_cache = cache if cache is not None else get_evidence_cache()
    key = evidence_cache_key(query, tier, k)
    try:
        cached = await active_cache.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Evidence cache get raised, falling back to live search: %r", exc)
        cached = None
    if cached is not None:
        return cached
    # Building the chain can itself raise (a misconfigured PROOV_SEARCH_PROVIDER, or `tavily`
    # selected with no key). The "never raises out" contract means a config error must degrade
    # to "no evidence" (claim → unverifiable, NFR3), not crash a paid order.
    try:
        chain = providers if providers is not None else default_search_chain()
    except SearchError as exc:
        logger.warning("Search provider chain unavailable, returning no evidence: %r", exc)
        return []
    # Harden the caller-supplied timeout exactly as the env value is hardened (reject
    # inf/nan/≤0) — an infinite timeout would defeat the per-call SLA bound (NFR2).
    per_call = _resolve_timeout(
        str(timeout) if timeout is not None else os.environ.get("PROOV_SEARCH_TIMEOUT")
    )

    deep = tier == "deep"
    merged: list[Evidence] = []  # Deep accumulator (unused for Quick).
    # Both the Quick first-non-empty path and the Deep merged-at-end path funnel through this
    # single local so the one cache-put + return below applies to both (Story 2.8).
    result: list[Evidence] = []
    for provider in chain:
        try:
            evidence = await asyncio.wait_for(provider.search(query, k), per_call)
        except SearchError as exc:
            logger.warning("Search provider failed, falling through: %r", exc)
            continue
        except asyncio.TimeoutError:
            logger.warning("Search provider timed out after %ss, falling through", per_call)
            continue
        except Exception as exc:  # noqa: BLE001
            # A pluggable provider may raise anything; the "never raises out" contract requires
            # we degrade, not crash. `asyncio.CancelledError` is a BaseException and is
            # intentionally NOT caught here, so task cancellation still propagates.
            logger.warning("Search provider raised unexpectedly, falling through: %r", exc)
            continue
        if not evidence:
            continue
        if deep:
            # Deep: accumulate across ALL providers; dedupe/cap happens once at the end.
            merged.extend(evidence)
        else:
            # Quick: first non-empty provider wins (cheapest).
            result = _normalise_evidence(evidence, k)
            break
    if deep and merged:
        result = _normalise_evidence(merged, k)
    # Cache only a non-empty result (Story 2.8): caching `[]` would persist a transient outage
    # as "no evidence" for the TTL. `put` is best-effort — a misbehaving injected cache must not
    # break the never-raises-out contract (CancelledError still propagates).
    if result:
        try:
            await active_cache.put(key, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence cache put raised, skipping store: %r", exc)
    return result

"""Citation check — verify buyer-provided `sources` (FR9, Story 2.4).

The fourth slice of the verification engine `[B]`. Given the `sources` a buyer supplied
with their output, this module checks each one on two axes and flags it
`ok`/`fabricated`/`misattributed` for the PRD §6 `citations_checked[]` field:

  1. **Retrievability** — a small, injectable `httpx` GET of the source URL (status < 400,
     redirects followed). A confirmed non-retrievable source is `fabricated` — the only
     verdict-flipping citation flag (FR10 `fail = ≥1 fabricated citation`, applied by the
     Story 2.5 aggregator, not here).
  2. **Support** — whether the retrieved source actually backs the output. This **reuses the
     existing `proov.llm.judge_claim` seam** (Story 2.3) — NO new `LLMProvider` method, NO
     change to `proov/llm.py`; citation support is the *third* consumer of that interface
     (extract → retrieve → judge), proving the seam composes. The submitted `output` is the
     synthetic "attached claim" (the flat PRD §6 `sources` list carries no per-source claim
     linkage — see the story's Open Question 2).

Precision over recall (NFR4): `fabricated` fires ONLY on a confirmed-unretrievable source,
`misattributed` ONLY on a positive `unsupported` judgment — never on mere uncertainty ("a
verifier that cries wolf is worse than useless", PRD §1). Like `retrieve_evidence`, the
top-level `check_citations` **never raises out** (degrade, don't drop — NFR3): a bad source
degrades to a conservative non-fabricated `ok`, not a crash and not a false `fabricated`.

Quick checks **provided sources only** (architecture §4); Deep (Story 2.7) ALSO covers the
**discovered** sources the retrieval surfaced — flagged at ZERO extra network/LLM cost by
reusing the stance the judge already assigned (no re-fetch, no re-judge), passed in via the
`discovered` kwarg. No `croo` import — engine `[B]` code; `httpx` is allowed (off the SDK path).
"""

from __future__ import annotations

import logging
import math
import os
import re

import httpx

from .llm import judge_claim
from .types import CitationCheck, CitationFlag, Claim, Evidence, Stance, Tier

logger = logging.getLogger("proov.citations")

# Bound the fetched source text fed to the support judge — payload / LLM-judge cost.
_MAX_FETCH_CHARS = 2000
# Bound the synthetic output-as-claim text handed to the judge.
_MAX_CLAIM_CHARS = 2000
# Per-source fetch timeout default; an infinite timeout would defeat the SLA (NFR2).
_DEFAULT_CITATION_TIMEOUT = 10.0

# Strip HTML tags and collapse whitespace so a fetched page becomes lean text for the judge.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _resolve_timeout(raw: str | None) -> float:
    """Parse `PROOV_CITATION_TIMEOUT`, tolerating garbage by falling back to the default.

    Hardened beat-for-beat like `proov.search._resolve_timeout`: rejects non-finite
    (inf/nan) as well as ≤0 — an infinite per-fetch timeout is "no timeout", which would let
    a hung citation fetch block a paid order past its SLA (NFR2).
    """
    if raw is None:
        return _DEFAULT_CITATION_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CITATION_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_CITATION_TIMEOUT
    return value


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace to clean text.

    Mirrors `proov.search._strip_html` (replicated rather than importing a private from
    `search.py` — keeps this module's small helper local; minor duplication noted as the
    story's Open Question on `_strip_html` reuse). A crude tag-strip, not a readability
    extractor: JS-rendered pages yield little text (→ blank content → `ok`), which is the
    intended v1 benefit-of-the-doubt behaviour.
    """
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub("", text)).strip()


async def _fetch_source(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float,
) -> tuple[bool, str]:
    """Retrievability probe: one async GET, NEVER raises out → `(retrievable, cleaned_text)`.

    Issues a single async `httpx` GET with `follow_redirects=True`. A response with
    `status_code < 400` (2xx / resolved-3xx) → `(True, cleaned)` where
    `cleaned = _strip_html(response.text)[:_MAX_FETCH_CHARS]`; `status_code >= 400`
    (4xx/5xx) → `(False, "")`; an `httpx.HTTPError` (transport / DNS / timeout) → `(False, "")`
    with a logged warning. We do NOT call `raise_for_status()` — we must *distinguish*
    retrievable from not, not raise on a 404.

    The `httpx.AsyncClient` is injectable so tests drive it via `httpx.MockTransport` with no
    real socket; when `None`, a client is opened with the resolved per-fetch timeout. These
    are arbitrary buyer URLs — no API key, no auth header, and the response body is NEVER
    logged (could be large/sensitive — log the exception repr / URL only; NFR5).
    """
    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as owned:
                response = await owned.get(url)
        else:
            # The injected client (test MockTransport) carries its own behaviour; still ask
            # for redirect-following so a 3xx chain resolves like the owned-client path.
            response = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        # Transport / DNS / timeout — a not-retrievable signal, not an exception out.
        logger.warning("citation fetch failed for %s: %r", url, exc)
        return (False, "")

    if response.status_code >= 400:
        logger.debug(
            "citation source %s not retrievable (status %s)", url, response.status_code
        )
        return (False, "")
    return (True, _strip_html(response.text)[:_MAX_FETCH_CHARS])


def _flag_for(retrievable: bool, status: str | None) -> tuple[bool, CitationFlag]:
    """Map a per-source `(retrievable, judge-status)` to `(supports_attached_claim, flag)`.

    The precision-over-recall heart of this story (NFR4 / PRD §1). `supports_attached_claim`
    is honest — it asserts support ONLY when the judge positively confirms it; `misattributed`
    fires ONLY on a positive contradiction, never on uncertainty:

      - not retrievable                → `(False, "fabricated")`  (verdict-flipping; FR10)
      - retrievable, `supported`       → `(True,  "ok")`
      - retrievable, `unsupported`     → `(False, "misattributed")`  (positively refuted)
      - retrievable, `unverifiable` / `None` / blank-content sentinel
                                       → `(False, "ok")`  (support UNCONFIRMED but we won't
        cry wolf on thin evidence — the one intentional `ok` + `supports=False` row; correct,
        not a bug — Open Question 3)

    `status=None` is the caller's "no judgment was made" sentinel (blank fetched content, or
    the judge degraded) — it lands in the final uncertainty branch.
    """
    if not retrievable:
        return (False, "fabricated")
    if status == "supported":
        return (True, "ok")
    if status == "unsupported":
        return (False, "misattributed")
    # "unverifiable" / None / blank-content → honest: support unconfirmed, but NOT misattributed.
    return (False, "ok")


async def check_citations(
    output: str,
    sources,
    tier: Tier,
    *,
    provider=None,
    client: httpx.AsyncClient | None = None,
    options: dict | None = None,
    timeout: float | None = None,
    discovered: list[tuple[str, Stance]] | None = None,
) -> list[CitationCheck]:
    """Check buyer-provided `sources` (+ Deep discovered), flag each, and NEVER raise out.

    Empty/missing `sources` → no provided checks (no fetch, no judge). Otherwise each source
    (a `{"url", "title"?}` dict per the validated PRD §6 input) is normalised — trim the url,
    drop blank-url entries, dedupe by url (first-seen wins), skip non-dict items — then, for
    each unique source, processed **sequentially** (a handful of sources; parallelism is
    Story 3.3): fetch → (map per `_flag_for`) → `CitationCheck`. The support judgment reuses
    `proov.llm.judge_claim` with the output as the synthetic "attached claim".

    **Degrade, don't drop (NFR3).** Each source is wrapped so one bad source cannot kill the
    batch: an unexpected per-source `Exception` is logged and degraded to the *non-crying-wolf*
    `CitationCheck(url, retrievable=True, supports_attached_claim=False, flag="ok")` — we
    cannot prove it fabricated, so we don't. `_fetch_source` already swallows `httpx.HTTPError`
    and `judge_claim` already swallows `LLMError`, so this wrapper is belt-and-suspenders for a
    misbehaving pluggable provider. `asyncio.CancelledError`/`BaseException` are intentionally
    NOT caught — task cancellation must propagate (mirrors `retrieve_evidence`).

    **Deep `discovered` (Story 2.7).** When `tier == "deep"` and `discovered` is non-empty, a
    `CitationCheck` is appended for each unique discovered `(url, stance)` AFTER the provided
    ones, flagged from the stance the judge ALREADY assigned during retrieval/judgment — at
    ZERO new network/LLM cost (NO re-fetch, NO re-judge). A discovered source the engine itself
    surfaced is honest evidence, so it is `retrievable=True` (it WAS retrieved → never
    `fabricated`) and `flag="ok"`; `supports_attached_claim` is `True` only for a `supports`
    stance (`refutes`/`neutral` → `ok` + not-supports; a discovered refuting source is honest
    evidence, NOT a buyer "misattribution" — `fabricated`/`misattributed` are reserved for
    buyer-PROVIDED citations, precision over recall). A discovered url equal to a provided one
    is NOT double-listed. For `tier == "quick"`, `discovered` is ignored (provided-only). The
    `fabricated` flag (provided-only) feeds the Story 2.5 verdict.
    """
    results: list[CitationCheck] = []

    if sources:
        # Normalise: drop non-dict / blank urls, dedupe by url (first-seen wins, order kept).
        seen: set[str] = set()
        normalised: list[tuple[str, str]] = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            # Defend the type, not just the presence: `_sources_ok` validates `url` is a str
            # but NOT `title`, and unknown callers may pass either un-typed. A non-str
            # `.strip()` here would raise OUTSIDE the per-source try/except and break "never
            # raises out".
            raw_url = item.get("url")
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            if not url or url in seen:
                continue
            seen.add(url)
            raw_title = item.get("title")
            title = raw_title.strip() if isinstance(raw_title, str) else ""
            normalised.append((url, title))

        if normalised:
            # Caller `timeout` overrides env (mirror `retrieve_evidence`'s caller-timeout).
            per_call = _resolve_timeout(
                str(timeout)
                if timeout is not None
                else os.environ.get("PROOV_CITATION_TIMEOUT")
            )

            for url, title in normalised:
                try:
                    retrievable, content = await _fetch_source(
                        url, client=client, timeout=per_call
                    )
                    if not retrievable:
                        # Not retrievable → fabricated. No support judgment (no spend).
                        supports, flag = _flag_for(False, None)
                    elif not content.strip():
                        # Retrievable but unreadable (JS-rendered / empty body) → ok, no judge.
                        supports, flag = _flag_for(True, None)
                    else:
                        judgment = await judge_claim(
                            Claim(id="citation-target", text=output[:_MAX_CLAIM_CHARS]),
                            [Evidence(source=url, title=title, snippet=content)],
                            tier,
                            provider=provider,
                            options=options,
                        )
                        supports, flag = _flag_for(True, judgment.status)
                except Exception as exc:  # noqa: BLE001
                    # Both helpers already never raise, but a pluggable provider could raise
                    # anything; the "never raises out" contract requires we degrade THIS
                    # source, not crash. We degrade to a conservative NON-fabricated `ok` —
                    # never cry wolf on an internal error. `asyncio.CancelledError` is a
                    # BaseException and is NOT caught here, so cancellation still propagates.
                    logger.warning(
                        "citation check degraded to ok after error for %s: %r", url, exc
                    )
                    supports, flag = (False, "ok")
                    retrievable = True

                results.append(CitationCheck(url, retrievable, supports, flag))

    # Deep "provided + discovered" (Story 2.7): append discovered sources, flagged from the
    # already-assigned stance at zero new cost. Excludes any url already listed (provided or an
    # earlier discovered dup). Never appended for Quick.
    if tier == "deep" and discovered:
        listed = {cc.source for cc in results}
        for raw_url, stance in discovered:
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            if not url or url in listed:
                continue
            listed.add(url)
            # Honest evidence the engine surfaced: retrievable (it WAS retrieved → not
            # fabricated), flag `ok`; only a `supports` stance asserts support.
            results.append(CitationCheck(url, True, stance == "supports", "ok"))

    return results

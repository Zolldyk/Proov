"""Citation check — verify buyer-provided `sources` (FR9, Story 2.4).

The fourth slice of the verification engine `[B]`. Given the `sources` a buyer supplied
with their output, this module checks each one on two axes and flags it
`ok`/`fabricated`/`misattributed` for the PRD §6 `citations_checked[]` field:

  1. **Retrievability** — a small, injectable `httpx` GET of the source URL (status < 400,
     redirects followed, browser-like `User-Agent`). Classified **three ways** (Story 3.1):
     `retrievable`, `absent` (a DEFINITIVE 404/410), or `ambiguous` (any other 4xx/5xx or a
     transport error). Only a confirmed-`absent` source is `fabricated` — the only
     verdict-flipping citation flag (FR10 `fail = ≥1 fabricated citation`, applied by the
     Story 2.5 aggregator, not here); an `ambiguous` source (paywalled / rate-limited /
     momentarily down) is the conservative `ok`, never a false `fabricated` (NFR4).
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

import asyncio
import ipaddress
import logging
import math
import os
import re
import socket
from typing import Literal

import httpx

from .llm import judge_claim
from .llm import _resolve_timeout as _resolve_llm_timeout
from .types import CitationCheck, CitationFlag, Claim, Evidence, Stance, Tier

logger = logging.getLogger("proov.citations")

# Bound the fetched source text fed to the support judge — payload / LLM-judge cost.
_MAX_FETCH_CHARS = 2000
# Story 3.3: bound the RAW body read before stripping/truncation, so one hostile/slow multi-MB
# source cannot spike memory/CPU. A response whose Content-Length exceeds this is treated as
# `ambiguous` (we cannot fairly judge a truncated giant); otherwise at most this many bytes are
# read (`response.content[:_MAX_FETCH_BYTES]`) before `_strip_html`/`_MAX_FETCH_CHARS`.
_MAX_FETCH_BYTES = 1_000_000
# Bound the synthetic output-as-claim text handed to the judge.
_MAX_CLAIM_CHARS = 2000
# Per-source fetch timeout default; an infinite timeout would defeat the SLA (NFR2).
_DEFAULT_CITATION_TIMEOUT = 10.0
# Story 4.4 SSRF guard: bound manual redirect following so a redirect loop cannot spin forever.
# A chain longer than this is refused (→ the non-fabricating `ambiguous` outcome).
_MAX_REDIRECTS = 5
# The cloud instance-metadata address (AWS/GCP/Azure all answer here). Already link-local, but
# rejected explicitly so the intent is unmistakable in the guard.
_CLOUD_METADATA_IP = "169.254.169.254"
# Story 3.4: cap the number of buyer-PROVIDED sources fetched+judged, closing the deferred-2.4
# paid-call amplification hole (a giant `sources` array → unbounded paid judge calls/fetches).
# Default 50 — generous vs typical buyer use + the 20/50 claim caps, still bounds spend.
_DEFAULT_MAX_SOURCES = 50

# Three-way retrievability classification (Story 3.1 precision fix). `retrievable` (the source
# resolved, status < 400), `absent` (a DEFINITIVE 404/410 — the source provably does not
# exist), or `ambiguous` (any other 4xx/5xx — 401/403/429/5xx — or a transport error / timeout
# / DNS failure: we cannot *prove* the source fabricated, so we must not cry wolf).
Retrievability = Literal["retrievable", "absent", "ambiguous"]

# Only a confirmed 404/410 maps to the verdict-flipping `fabricated` flag (NFR4 precision over
# recall). A paywalled 403, rate-limited 429, momentary 503, or flaky timeout is `ambiguous` →
# the conservative `ok`, never a false `fail` (the deferred-work 2.4 OQ1 item, fixed here).
_DEFINITIVE_ABSENT: frozenset[int] = frozenset({404, 410})

# A browser-like default User-Agent so a bot-blocking 403 is avoided where possible (override
# via `PROOV_CITATION_USER_AGENT`). These are arbitrary buyer URLs — still no API key/auth.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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


def _resolve_max_sources(raw: str | None = None) -> int:
    """Resolve `PROOV_MAX_SOURCES`, tolerating garbage by falling back to the default (Story 3.4).

    The buyer-provided-source cap that bounds paid-call amplification (deferred 2.4). Mirrors the
    `_resolve_*` hardening idiom: a missing / non-int / ≤0 value → `_DEFAULT_MAX_SOURCES` (a
    zero/negative cap would silently drop ALL provided sources, the opposite of intended). Returns
    a positive `int`.
    """
    raw = raw if raw is not None else os.environ.get("PROOV_MAX_SOURCES")
    if raw is None:
        return _DEFAULT_MAX_SOURCES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SOURCES
    if value <= 0:
        return _DEFAULT_MAX_SOURCES
    return value


def _resolve_user_agent(raw: str | None = None) -> str:
    """Resolve the citation-fetch `User-Agent`: `PROOV_CITATION_USER_AGENT` or the browser default.

    A blank / unset value falls back to `_DEFAULT_USER_AGENT` (a real desktop UA string) so a
    bot-blocking 403 is avoided where possible — under the new classification a 403 is only
    `ambiguous` (→ `ok`) anyway, but a successful fetch lets us actually judge support.
    """
    candidate = raw if raw is not None else os.environ.get("PROOV_CITATION_USER_AGENT")
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return _DEFAULT_USER_AGENT


def _classify_retrievability(
    status_code: int | None, *, transport_error: bool
) -> Retrievability:
    """Map an HTTP status / transport outcome to the three-way `Retrievability` (PURE).

    The precision lever (NFR4). A transport error / timeout / DNS failure (or an absent status)
    is `ambiguous` — we reached no verdict on existence; a DEFINITIVE 404/410 is `absent` (the
    source provably does not exist → the only path to `fabricated`); `status < 400` is
    `retrievable`; every other 4xx/5xx (401/403/429/5xx) is `ambiguous` — restricted or
    transiently unavailable, NOT proof of fabrication. No I/O, no env — same inputs ⇒ same class.
    """
    if transport_error or status_code is None:
        return "ambiguous"
    if status_code in _DEFINITIVE_ABSENT:
        return "absent"
    if status_code < 400:
        return "retrievable"
    return "ambiguous"


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace to clean text.

    Mirrors `proov.search._strip_html` (replicated rather than importing a private from
    `search.py` — keeps this module's small helper local; minor duplication noted as the
    story's Open Question on `_strip_html` reuse). A crude tag-strip, not a readability
    extractor: JS-rendered pages yield little text (→ blank content → `ok`), which is the
    intended v1 benefit-of-the-doubt behaviour.
    """
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub("", text)).strip()


def _resolve_host(host: str) -> list[str]:
    """Resolve `host` to its IP address strings via `socket.getaddrinfo` — the monkeypatchable seam.

    The ONE DNS touchpoint of the SSRF guard (tests monkeypatch THIS, never a real socket). A
    resolution failure (DNS error / bad host) returns `[]` — NOT a block: a host that won't
    resolve degrades to the existing `ambiguous` transport outcome via the real fetch, it is not
    "proof" of an internal target. Returns every resolved address so a multi-record host with even
    one internal A/AAAA record is caught by the caller (a DNS-rebinding defence).
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return [info[4][0] for info in infos]


def _is_blocked_address(ip: str) -> bool:
    """True if `ip` is a private/loopback/link-local/reserved/metadata target (SSRF, AC7) — PURE.

    Stdlib `ipaddress` classification, no I/O. An IPv4-mapped IPv6 address (`::ffff:127.0.0.1`)
    is unwrapped first so the wrapper cannot smuggle an internal v4 target past the v4 checks. The
    cloud-metadata `169.254.169.254` is already link-local but is also rejected explicitly. An
    unparseable address string is blocked conservatively (we could not prove it safe).
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or str(addr) == _CLOUD_METADATA_IP
    )


def _ssrf_block_reason(url: str) -> str | None:
    """Return a reason string if `url` must be REFUSED by the SSRF guard (AC7), else `None`.

    Two gates: (1) the scheme must be `http`/`https` — `file://`, `gopher://`, `ftp://` etc. are
    refused outright; (2) the host is resolved (`_resolve_host`) and refused if ANY resolved IP is
    private/loopback/link-local/reserved/metadata (`_is_blocked_address`). A resolution failure is
    NOT a block (it falls through to the real fetch → `ambiguous`). PURE except the DNS seam — same
    url ⇒ same decision for a fixed resolver. This is called on the INITIAL url AND on every
    redirect `Location`, so a 302-to-internal / DNS-rebinding cannot bypass a one-shot check.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return "unparseable url"
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return f"non-http(s) scheme {scheme!r}"
    host = parsed.host
    if not host:
        return "missing host"
    for ip in _resolve_host(host):
        if _is_blocked_address(ip):
            return f"host {host!r} resolves to blocked address {ip}"
    return None


async def _get_guarded(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> httpx.Response | None:
    """GET `url`, following redirects MANUALLY with an SSRF re-check on EVERY hop (AC7).

    Returns the final non-redirect `httpx.Response`, or `None` when the initial url or any redirect
    target is refused by `_ssrf_block_reason` (or the chain exceeds `_MAX_REDIRECTS`) — the caller
    degrades a `None` to the conservative `ambiguous` (a blocked fetch can NOT prove a citation
    fabricated). Redirects are followed by hand (`follow_redirects=False` + `response.next_request`)
    precisely so a 302-to-internal is caught — `follow_redirects=True` would chase it before any
    re-check. Transport errors (`httpx.HTTPError`) propagate to the caller's handler.
    """
    reason = _ssrf_block_reason(url)
    if reason:
        logger.warning("citation fetch blocked (SSRF guard) for %s: %s", url, reason)
        return None
    response = await client.get(url, headers=headers, follow_redirects=False)
    hops = 0
    while response.is_redirect and response.next_request is not None:
        next_req = response.next_request
        next_url = str(next_req.url)
        hops += 1
        await response.aclose()
        # Bail on an over-long chain BEFORE resolving the next hop, so a too-deep redirect cannot
        # provoke a DNS lookup of an attacker-controlled `Location` host we are about to refuse.
        if hops > _MAX_REDIRECTS:
            logger.warning(
                "citation fetch for %s exceeded %d redirects → blocked", url, _MAX_REDIRECTS
            )
            return None
        reason = _ssrf_block_reason(next_url)
        if reason:
            logger.warning(
                "citation redirect blocked (SSRF guard) %s → %s: %s", url, next_url, reason
            )
            return None
        response = await client.send(next_req, follow_redirects=False)
    if response.is_redirect:
        # A 3xx we could not follow (no / invalid `Location` → `next_request is None`) is not a
        # retrievable source. Close it and degrade to the conservative `ambiguous` rather than
        # letting `_classify_retrievability` map a bare 3xx (`status < 400`) to `retrievable`.
        await response.aclose()
        return None
    return response


async def _fetch_source(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float,
) -> tuple[Retrievability, str]:
    """Retrievability probe: one SSRF-guarded GET, NEVER raises out → `(Retrievability, cleaned_text)`.

    Issues an async `httpx` GET behind the Story 4.4 SSRF guard (`_get_guarded`): the url and
    every redirect `Location` are screened (`_ssrf_block_reason`) and redirects are followed
    MANUALLY so a 302-to-internal / DNS-rebinding cannot reach a private/loopback/link-local/
    cloud-metadata target. A blocked url → `("ambiguous", "")` (a refused fetch can NOT prove a
    source fabricated — never a false `fabricated`, never raises). A browser-like `User-Agent`
    rides every hop (Story 3.1). The (final) response status / transport outcome is classified
    three ways via `_classify_retrievability`: `retrievable` (status < 400) →
    `("retrievable", _strip_html(text)[:_MAX_FETCH_CHARS])`; `absent` (404/410) or any `ambiguous`
    (other 4xx/5xx) → `(cls, "")`; an `httpx.HTTPError` (transport / DNS / timeout) →
    `("ambiguous", "")` with a logged warning. We do NOT call `raise_for_status()` — we must
    *distinguish* the classes, not raise on a 404.

    The `httpx.AsyncClient` is injectable so tests drive it via `httpx.MockTransport` with no
    real socket; when `None`, a client is opened with the resolved per-fetch timeout. The UA
    header is set per-request so it rides BOTH the owned and the injected client. These are
    arbitrary buyer URLs — no API key, no auth header, and the response body is NEVER logged
    (could be large/sensitive — log the exception repr / URL only; NFR5).
    """
    headers = {"User-Agent": _resolve_user_agent()}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                response = await _get_guarded(owned, url, headers)
        else:
            response = await _get_guarded(client, url, headers)
    except httpx.HTTPError as exc:
        # Transport / DNS / timeout — ambiguous (we reached no verdict on existence), not an
        # exception out, and NOT proof of fabrication.
        logger.warning("citation fetch failed for %s: %r", url, exc)
        return ("ambiguous", "")

    if response is None:
        # The SSRF guard refused the url or a redirect hop (or the chain was too long). A blocked
        # fetch can NOT prove a citation fabricated — degrade to the conservative `ambiguous`.
        return ("ambiguous", "")

    cls = _classify_retrievability(response.status_code, transport_error=False)
    if cls == "retrievable":
        # Story 3.3: bound the body read so a hostile/giant source cannot spike memory/CPU. A
        # declared `Content-Length` over the cap → `ambiguous` (we cannot fairly judge a
        # truncated giant; a timeout/oversize cannot prove `fabricated`). Otherwise read at most
        # `_MAX_FETCH_BYTES` of the raw body (instead of `response.text` over the WHOLE body)
        # before stripping/truncating.
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _MAX_FETCH_BYTES:
                    logger.debug(
                        "citation source %s oversize (content-length %s > %d) → ambiguous",
                        url,
                        declared,
                        _MAX_FETCH_BYTES,
                    )
                    return ("ambiguous", "")
            except (TypeError, ValueError):
                pass  # an unparseable header → fall through to the bounded read
        raw = response.content[:_MAX_FETCH_BYTES]
        text = raw.decode(response.encoding or "utf-8", "replace")
        return (cls, _strip_html(text)[:_MAX_FETCH_CHARS])
    logger.debug(
        "citation source %s classified %s (status %s)", url, cls, response.status_code
    )
    return (cls, "")


def _flag_for(cls: Retrievability, status: str | None) -> tuple[bool, bool, CitationFlag]:
    """Map a per-source `(Retrievability, judge-status)` → `(retrievable, supports, flag)`.

    The precision-over-recall heart of this story (NFR4 / PRD §1) and the SINGLE classification
    → flag mapping (shared by `check_citations` and the calibration replay). `supports` is
    honest — asserted ONLY when the judge positively confirms it; `fabricated` fires ONLY on a
    confirmed-`absent` source, `misattributed` ONLY on a positive contradiction — never on mere
    uncertainty:

      - `absent`      (404/410)              → `(False, False, "fabricated")`  (verdict-flipping; FR10)
      - `ambiguous`   (other 4xx/5xx/timeout) → `(False, False, "ok")`  (cannot prove fabricated —
        paywalled / rate-limited / transiently down; conservative, NEVER `misattributed`)
      - `retrievable`, `supported`           → `(True,  True,  "ok")`
      - `retrievable`, `unsupported`         → `(True,  False, "misattributed")`  (positively refuted)
      - `retrievable`, `unverifiable` / `None` / blank-content sentinel
                                             → `(True,  False, "ok")`  (support UNCONFIRMED but we
        won't cry wolf on thin evidence — the intentional `ok` + `supports=False` row)

    `status=None` on the `retrievable` path is the caller's "no judgment was made" sentinel
    (blank fetched content, or the judge degraded) — it lands in the final uncertainty branch.
    The first tuple element (`retrievable`) is exactly `cls == "retrievable"`, the value the
    `CitationCheck.retrievable` field carries.
    """
    if cls == "absent":
        return (False, False, "fabricated")
    if cls == "ambiguous":
        # Cannot prove fabricated — support unconfirmed, but NOT a verdict-flipping flag.
        return (False, False, "ok")
    # retrievable → judge the fetched content for support.
    if status == "supported":
        return (True, True, "ok")
    if status == "unsupported":
        return (True, False, "misattributed")
    # "unverifiable" / None / blank-content → honest: support unconfirmed, but NOT misattributed.
    return (True, False, "ok")


async def _check_one_source(
    url: str,
    title: str,
    output: str,
    tier: Tier,
    *,
    provider,
    client: httpx.AsyncClient | None,
    timeout: float,
    options: dict | None,
) -> tuple[bool, bool, CitationFlag]:
    """Fetch + (judge) ONE source → `(retrievable, supports, flag)` — the wrappable unit (3.3).

    Factored out of `check_citations`'s loop so the whole fetch+judge for a source can be bounded
    by a single `asyncio.wait_for` total-time wall (Story 3.3): a redirect chain plus a slow
    support judge cannot together exceed a bounded wall. A `retrievable` source with non-blank
    content is judged for support (the third consumer of `judge_claim`); everything else
    short-circuits with no judge spend. Raises only what the caller bounds/propagates
    (`TimeoutError`/`CancelledError`); `_fetch_source` and `judge_claim` already swallow their own
    transport/LLM errors.
    """
    cls, content = await _fetch_source(url, client=client, timeout=timeout)
    if cls == "retrievable" and content.strip():
        judgment = await judge_claim(
            Claim(id="citation-target", text=output[:_MAX_CLAIM_CHARS]),
            [Evidence(source=url, title=title, snippet=content)],
            tier,
            provider=provider,
            options=options,
        )
        status = judgment.status
    else:
        # absent → fabricated; ambiguous → ok; retrievable+blank → ok. No spend.
        status = None
    return _flag_for(cls, status)


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
    max_paid_sources: int | None = None,
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

        # Story 3.4 source cap: truncate the deduped provided list to `PROOV_MAX_SOURCES` BEFORE
        # the paid fetch+judge loop, so a buyer cannot amplify outbound fetches / paid judge calls
        # by submitting an arbitrarily long `sources` array (deferred-2.4). Provided-only — the
        # Deep `discovered` appends below are zero-cost (already judged, no re-fetch/re-judge).
        max_sources = _resolve_max_sources()
        if len(normalised) > max_sources:
            logger.warning(
                "provided sources (%d) exceed PROOV_MAX_SOURCES (%d); dropping %d → capping",
                len(normalised),
                max_sources,
                len(normalised) - max_sources,
            )
            normalised = normalised[:max_sources]

        # Story 3.4 cost ceiling: the engine passes how many provided sources the REMAINING
        # per-order budget can afford (one paid judge call per retrievable source). Cap the paid
        # loop conservatively so the citation check can never push the order past its ceiling — the
        # surplus provided sources are dropped (degrade, mirroring the SLA early-stop → partial).
        # `None` (disabled meter) leaves the list untouched.
        if max_paid_sources is not None and len(normalised) > max_paid_sources:
            logger.warning(
                "provided sources (%d) exceed the remaining per-order cost budget (%d affordable); "
                "dropping %d to honor the cost ceiling",
                len(normalised),
                max_paid_sources,
                len(normalised) - max_paid_sources,
            )
            normalised = normalised[:max_paid_sources]

        if normalised:
            # Caller `timeout` overrides env (mirror `retrieve_evidence`'s caller-timeout).
            per_call = _resolve_timeout(
                str(timeout)
                if timeout is not None
                else os.environ.get("PROOV_CITATION_TIMEOUT")
            )
            # Story 3.3 per-source TOTAL-time bound: fetch (≤ per_call) + judge (≤ llm_timeout).
            # One slow/hostile source — a redirect chain plus a slow judge — cannot exceed this
            # wall (mirrors `retrieve_evidence`'s `wait_for`). Reuses the two already-resolved
            # timeouts rather than adding a new env knob (Open Question 7).
            total = per_call + _resolve_llm_timeout(os.environ.get("PROOV_LLM_TIMEOUT"))

            for url, title in normalised:
                try:
                    retrievable, supports, flag = await asyncio.wait_for(
                        _check_one_source(
                            url,
                            title,
                            output,
                            tier,
                            provider=provider,
                            client=client,
                            timeout=per_call,
                            options=options,
                        ),
                        total,
                    )
                except asyncio.TimeoutError:
                    # A timeout cannot prove `fabricated` (the precision contract) — degrade THIS
                    # source to the conservative non-crying-wolf `ok`, support unconfirmed.
                    logger.warning(
                        "citation check timed out for %s after %ss; degrading to ok",
                        url,
                        total,
                    )
                    retrievable, supports, flag = (True, False, "ok")
                except Exception as exc:  # noqa: BLE001
                    # Both helpers already never raise, but a pluggable provider could raise
                    # anything; the "never raises out" contract requires we degrade THIS
                    # source, not crash. We degrade to a conservative NON-fabricated `ok` —
                    # never cry wolf on an internal error. `asyncio.CancelledError` is a
                    # BaseException and is NOT caught here, so cancellation still propagates.
                    logger.warning(
                        "citation check degraded to ok after error for %s: %r", url, exc
                    )
                    retrievable, supports, flag = (True, False, "ok")

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

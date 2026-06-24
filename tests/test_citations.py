"""Tests for the citation-check layer (`proov/citations.py`) — OFFLINE ONLY.

No test opens a real socket or calls Gemini: `_fetch_source` is driven by an injected
`httpx.MockTransport` returning canned responses, and the support judgment uses the offline
`StubLLMProvider` (deterministic `ok`) plus a small `_CitationJudgeSpy` for the
`unsupported`/`unverifiable`/no-call branches. No keys anywhere on the fetch path — these are
arbitrary buyer URLs. Harness mirrors `tests/test_search.py` / `tests/test_llm.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from proov import citations as cit
from proov.citations import (
    _MAX_FETCH_BYTES,
    _MAX_FETCH_CHARS,
    _DEFAULT_CITATION_TIMEOUT,
    _DEFAULT_MAX_SOURCES,
    _DEFAULT_USER_AGENT,
    _classify_retrievability,
    _fetch_source,
    _flag_for,
    _is_blocked_address,
    _resolve_max_sources,
    _resolve_timeout,
    _resolve_user_agent,
    _ssrf_block_reason,
    _strip_html,
    check_citations,
)
from proov.llm import StubLLMProvider
from proov.types import CitationCheck, Claim, Evidence, Judgment


@pytest.fixture(autouse=True)
def _clear_citation_env(monkeypatch):
    """Citation env must not leak between tests (the entrypoint reads it directly)."""
    monkeypatch.delenv("PROOV_CITATION_TIMEOUT", raising=False)
    monkeypatch.delenv("PROOV_CITATION_USER_AGENT", raising=False)
    monkeypatch.delenv("PROOV_LLM_PROVIDER", raising=False)


@pytest.fixture(autouse=True)
def _stub_resolver(monkeypatch):
    """Pin the SSRF guard's DNS seam to a PUBLIC IP for all hosts — no real socket (Story 4.4).

    Every fetch test passes a public-looking host (`example.com`, `a`, `big.example`); without
    this they would do a real `getaddrinfo`. Stubbing `_resolve_host` to a single public address
    keeps them offline AND unblocked by the new guard. SSRF tests override this per-test to return
    an internal address (or assert host classification directly)."""
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34"])


# --------------------------------------------------------------------------- helpers


def _mock_fetch_client(handler) -> httpx.AsyncClient:
    """An httpx.AsyncClient wired to an injected MockTransport (no real socket)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _CitationJudgeSpy:
    """Duck-typed `LLMProvider` for the support path: returns a canned `Judgment`, counts calls.

    Used for the `misattributed`/`unverifiable`/no-call branches. It is duck-typed into the
    top-level `judge_claim` (never `isinstance`-checked), so it need not implement
    `extract_claims`.
    """

    def __init__(self, status: str = "supported") -> None:
        self.calls: list[tuple[Claim, list[Evidence]]] = []
        self._status = status

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment:
        self.calls.append((claim, list(evidence)))
        # Confidence is irrelevant to the flag mapping; only `status` is read.
        return Judgment(self._status, 0.5, ())


def _html(body: str) -> str:
    return f"<html><body><p>{body}</p></body></html>"


# --------------------------------------------------------------------------- types


def test_citation_check_is_frozen_with_prd_field_shape():
    cc = CitationCheck(
        source="https://a",
        retrievable=True,
        supports_attached_claim=False,
        flag="ok",
    )
    # Exact PRD §6 field order/shape: {source, retrievable, supports_attached_claim, flag}.
    assert [f.name for f in dataclasses.fields(cc)] == [
        "source",
        "retrievable",
        "supports_attached_claim",
        "flag",
    ]
    # Frozen / hashable like the other engine types.
    with pytest.raises(dataclasses.FrozenInstanceError):
        cc.flag = "fabricated"  # type: ignore[misc]
    assert hash(cc) == hash(
        CitationCheck("https://a", True, False, "ok")
    )


# --------------------------------------------------------------------------- _resolve_timeout / _strip_html


def test_resolve_timeout_handles_garbage_and_valid():
    assert _resolve_timeout(None) == _DEFAULT_CITATION_TIMEOUT
    assert _resolve_timeout("not-a-number") == _DEFAULT_CITATION_TIMEOUT
    assert _resolve_timeout("0") == _DEFAULT_CITATION_TIMEOUT
    assert _resolve_timeout("-5") == _DEFAULT_CITATION_TIMEOUT
    assert _resolve_timeout("inf") == _DEFAULT_CITATION_TIMEOUT
    assert _resolve_timeout("nan") == _DEFAULT_CITATION_TIMEOUT
    assert _resolve_timeout("3.5") == 3.5


def test_strip_html_removes_tags_and_collapses_whitespace():
    assert _strip_html("<p>Hello   <b>world</b></p>\n\n  done") == "Hello world done"


# ----------------------------------------------- _classify_retrievability (Story 3.1 precision fix)


def test_classify_retrievability_three_way():
    # status < 400 -> retrievable.
    assert _classify_retrievability(200, transport_error=False) == "retrievable"
    assert _classify_retrievability(204, transport_error=False) == "retrievable"
    # DEFINITIVE 404/410 -> absent (the only path to `fabricated`).
    assert _classify_retrievability(404, transport_error=False) == "absent"
    assert _classify_retrievability(410, transport_error=False) == "absent"
    # Any other 4xx/5xx (restricted / transient) -> ambiguous, NOT fabricated.
    for status in (401, 403, 429, 500, 503):
        assert _classify_retrievability(status, transport_error=False) == "ambiguous"
    # Transport error / missing status -> ambiguous (we reached no verdict on existence).
    assert _classify_retrievability(None, transport_error=True) == "ambiguous"
    assert _classify_retrievability(None, transport_error=False) == "ambiguous"


def test_resolve_user_agent_default_and_override():
    assert _resolve_user_agent(None) == _DEFAULT_USER_AGENT
    assert _resolve_user_agent("   ") == _DEFAULT_USER_AGENT
    assert _resolve_user_agent("MyBot/1.0") == "MyBot/1.0"


# --------------------------------------------------------------------------- _fetch_source


async def test_fetch_source_200_returns_retrievable_stripped_bounded_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("Paris is the capital of France."))

    async with _mock_fetch_client(handler) as client:
        cls, text = await _fetch_source(
            "https://example.com", client=client, timeout=1.0
        )
    assert cls == "retrievable"
    assert text == "Paris is the capital of France."
    assert "<" not in text  # tags stripped


async def test_fetch_source_bounds_to_max_fetch_chars():
    long_body = "x" * (_MAX_FETCH_CHARS + 500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=long_body)

    async with _mock_fetch_client(handler) as client:
        cls, text = await _fetch_source(
            "https://example.com", client=client, timeout=1.0
        )
    assert cls == "retrievable"
    assert len(text) == _MAX_FETCH_CHARS


@pytest.mark.parametrize("status", [404, 410])
async def test_fetch_source_404_410_is_absent(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("https://example.com", client=client, timeout=1.0)
    assert result == ("absent", "")  # definitive not-found -> the only fabricated path


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_fetch_source_other_4xx_5xx_is_ambiguous(status):
    # The precision fix: restricted / transient statuses are NO LONGER treated as fabricated.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("https://example.com", client=client, timeout=1.0)
    assert result == ("ambiguous", "")


async def test_fetch_source_transport_errors_are_ambiguous():
    # A transport failure can NOT prove a source fabricated -> ambiguous (was previously dropped
    # into the fabricated path via `(False, "")`).
    def connect_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns boom", request=request)

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    async with _mock_fetch_client(connect_handler) as client:
        assert await _fetch_source("https://a", client=client, timeout=1.0) == ("ambiguous", "")
    async with _mock_fetch_client(timeout_handler) as client:
        assert await _fetch_source("https://b", client=client, timeout=1.0) == ("ambiguous", "")


async def test_fetch_source_follows_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, text=_html("final page"))

    async with _mock_fetch_client(handler) as client:
        cls, text = await _fetch_source(
            "https://example.com/old", client=client, timeout=1.0
        )
    assert cls == "retrievable"
    assert text == "final page"


async def test_fetch_source_sends_browser_user_agent():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text=_html("page"))

    async with _mock_fetch_client(handler) as client:
        await _fetch_source("https://example.com", client=client, timeout=1.0)
    assert seen["ua"] == _DEFAULT_USER_AGENT


async def test_fetch_source_honours_user_agent_override(monkeypatch):
    monkeypatch.setenv("PROOV_CITATION_USER_AGENT", "ProovBot/9.9")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text=_html("page"))

    async with _mock_fetch_client(handler) as client:
        await _fetch_source("https://example.com", client=client, timeout=1.0)
    assert seen["ua"] == "ProovBot/9.9"


# ------------------------------------------- _flag_for: three-way classification -> (retr, supports, flag)


def test_flag_for_all_rows():
    # absent (404/410) -> the only fabricated path.
    assert _flag_for("absent", None) == (False, False, "fabricated")
    # ambiguous (other 4xx/5xx / timeout) -> ok, NOT fabricated (the precision fix).
    assert _flag_for("ambiguous", None) == (False, False, "ok")
    # retrievable -> judge the fetched content for support.
    assert _flag_for("retrievable", "supported") == (True, True, "ok")
    assert _flag_for("retrievable", "unsupported") == (True, False, "misattributed")
    assert _flag_for("retrievable", "unverifiable") == (True, False, "ok")
    assert _flag_for("retrievable", None) == (True, False, "ok")


# --------------------------------------------------------------------------- check_citations end-to-end


async def test_empty_sources_returns_empty_no_fetch_no_judge():
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("fetch should not be called for empty sources")

    async with _mock_fetch_client(handler) as client:
        for sources in ([], None):
            result = await check_citations(
                "out", sources, "quick", provider=spy, client=client
            )
            assert result == []
    assert spy.calls == []  # judge never called


async def test_fabricated_source_short_circuits_without_judge():
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://gone.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://gone.example", False, False, "fabricated")]
    assert spy.calls == []  # no support judgment attempted for a non-retrievable source


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_ambiguous_source_is_ok_not_fabricated_without_judge(status):
    # The precision fix end-to-end: a restricted / transient source is NOT a false `fabricated`
    # (which would flip the verdict to `fail`). It degrades to a conservative non-crying-wolf
    # `ok` with support unconfirmed, and no support judgment is spent.
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="blocked")

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://restricted.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://restricted.example", False, False, "ok")]
    assert spy.calls == []  # ambiguous -> no judge spend


async def test_timeout_source_is_ok_not_fabricated():
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://slow.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://slow.example", False, False, "ok")]
    assert spy.calls == []


async def test_retrievable_supported_source_is_ok():
    # StubLLMProvider returns `supported` for any non-empty evidence → ok + supports=True.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("backs the output"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://good.example", "title": "Good"}],
            "quick",
            provider=StubLLMProvider(),
            client=client,
        )
    assert result == [CitationCheck("https://good.example", True, True, "ok")]


async def test_unsupported_judge_yields_misattributed():
    spy = _CitationJudgeSpy(status="unsupported")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("unrelated content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://wrong.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://wrong.example", True, False, "misattributed")]
    assert len(spy.calls) == 1


async def test_unverifiable_judge_yields_ok_with_supports_false():
    spy = _CitationJudgeSpy(status="unverifiable")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("ambiguous content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://maybe.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    # The intentional ok + supports=False row.
    assert result == [CitationCheck("https://maybe.example", True, False, "ok")]


async def test_blank_body_is_ok_without_judge_call():
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="   \n  ")  # whitespace only → blank after strip

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://blank.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://blank.example", True, False, "ok")]
    assert spy.calls == []  # couldn't read it → no judge call


async def test_duplicate_urls_collapse_to_one():
    spy = _CitationJudgeSpy(status="supported")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [
                {"url": "https://dup.example", "title": "first"},
                {"url": "  https://dup.example  ", "title": "second"},
                {"url": "https://dup.example"},
            ],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://dup.example", True, True, "ok")]
    assert len(spy.calls) == 1  # deduped before fetch/judge


async def test_blank_and_non_dict_sources_are_skipped():
    spy = _CitationJudgeSpy(status="supported")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            ["not-a-dict", {"url": "   "}, {"title": "no url"}, {"url": "https://ok.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://ok.example", True, True, "ok")]


async def test_non_string_url_or_title_does_not_raise_out():
    # `_sources_ok` validates `url` is a str but NOT `title`, so a non-str `title` (or a `url`
    # from a validation-bypassing caller) reaches the normalise loop. A non-str `.strip()` there
    # would raise OUTSIDE the per-source try/except and break "never raises out" (NFR3). The
    # malformed entries must be skipped (not crash), and a well-formed entry still processed.
    spy = _CitationJudgeSpy(status="supported")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [
                {"url": "https://x.example", "title": 123},  # non-str title (passes _sources_ok)
                {"url": 456},  # non-str url (validation-bypassing caller)
                {"url": "https://ok.example", "title": "fine"},
            ],
            "quick",
            provider=spy,
            client=client,
        )
    # Non-str url is dropped; non-str title is coerced to "" and the source is still checked.
    assert result == [
        CitationCheck("https://x.example", True, True, "ok"),
        CitationCheck("https://ok.example", True, True, "ok"),
    ]


async def test_one_raising_source_degrades_and_does_not_abort_batch():
    spy = _CitationJudgeSpy(status="supported")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boom.example":
            raise RuntimeError("unexpected non-httpx error")
        return httpx.Response(200, text=_html("good content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://boom.example"}, {"url": "https://good.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    # The raising source degrades to a conservative NON-fabricated ok; the batch continues.
    assert result == [
        CitationCheck("https://boom.example", True, False, "ok"),
        CitationCheck("https://good.example", True, True, "ok"),
    ]


async def test_check_citations_never_raises():
    # A handler that always raises an unexpected error must not bubble out of check_citations.
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("kaboom")

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://x.example"}],
            "quick",
            provider=StubLLMProvider(),
            client=client,
        )
    assert result == [CitationCheck("https://x.example", True, False, "ok")]


# ----------------------------------------------- Deep provided + discovered (Story 2.7)


async def test_deep_appends_discovered_after_provided_no_new_fetch_or_judge():
    spy = _CitationJudgeSpy(status="supported")
    fetches = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fetches["n"] += 1
        return httpx.Response(200, text=_html("backs the output"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://provided.example"}],
            "deep",
            provider=spy,
            client=client,
            discovered=[
                ("https://disc-supports.example", "supports"),
                ("https://disc-refutes.example", "refutes"),
                ("https://disc-neutral.example", "neutral"),
            ],
        )
    # Provided first, then discovered appended in order.
    assert result == [
        CitationCheck("https://provided.example", True, True, "ok"),
        CitationCheck("https://disc-supports.example", True, True, "ok"),
        CitationCheck("https://disc-refutes.example", True, False, "ok"),
        CitationCheck("https://disc-neutral.example", True, False, "ok"),
    ]
    # Discovered cost NOTHING extra: only the one provided source was fetched, and the judge
    # was only ever handed the provided url's evidence — no discovered url was re-fetched or
    # re-judged. (The provided source's support judgment is itself Deep multi-pass.)
    assert fetches["n"] == 1
    judged_sources = {ev.source for _claim, evs in spy.calls for ev in evs}
    assert judged_sources == {"https://provided.example"}


async def test_deep_discovered_never_fabricated_or_misattributed():
    # A discovered refuting/neutral source is honest evidence — never fabricated/misattributed.
    result = await check_citations(
        "the output",
        None,
        "deep",
        discovered=[("https://r.example", "refutes")],
    )
    assert result == [CitationCheck("https://r.example", True, False, "ok")]
    assert all(cc.flag == "ok" for cc in result)


async def test_deep_discovered_not_double_listed_when_equal_to_provided():
    spy = _CitationJudgeSpy(status="supported")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://same.example"}],
            "deep",
            provider=spy,
            client=client,
            discovered=[
                ("https://same.example", "supports"),  # equals the provided url → skip
                ("https://other.example", "supports"),
            ],
        )
    assert result == [
        CitationCheck("https://same.example", True, True, "ok"),  # the PROVIDED check wins
        CitationCheck("https://other.example", True, True, "ok"),
    ]


async def test_deep_discovered_dedupes_among_themselves():
    result = await check_citations(
        "out",
        None,
        "deep",
        discovered=[
            ("https://dup.example", "supports"),
            ("https://dup.example", "refutes"),  # first-seen wins → supports
            ("https://x.example", "neutral"),
        ],
    )
    assert result == [
        CitationCheck("https://dup.example", True, True, "ok"),
        CitationCheck("https://x.example", True, False, "ok"),
    ]


async def test_quick_ignores_discovered():
    spy = _CitationJudgeSpy(status="supported")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("content"))

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://provided.example"}],
            "quick",
            provider=spy,
            client=client,
            discovered=[("https://disc.example", "supports")],
        )
    # Quick is provided-only — the discovered source is NOT listed.
    assert result == [CitationCheck("https://provided.example", True, True, "ok")]


# ---------------------------------------------- per-source bounds (Story 3.3)


async def test_per_source_total_bound_degrades_to_ok(monkeypatch):
    # AC7: a source whose fetch+judge exceeds the per-source total-time wall is bounded by
    # `asyncio.wait_for` → degraded to the non-crying-wolf `ok` (a timeout cannot prove
    # `fabricated`), no judge spent. Proven by hanging `_fetch_source` under a tiny total budget.
    never = asyncio.Event()

    async def _hang_fetch(url, *, client=None, timeout):
        await never.wait()  # never completes → the wait_for bound trips

    monkeypatch.setattr(cit, "_fetch_source", _hang_fetch)
    monkeypatch.setenv("PROOV_CITATION_TIMEOUT", "0.01")
    monkeypatch.setenv("PROOV_LLM_TIMEOUT", "0.01")  # total = 0.02s

    spy = _CitationJudgeSpy()
    result = await check_citations(
        "out", [{"url": "https://slow.example"}], "quick", provider=spy
    )
    assert result == [CitationCheck("https://slow.example", True, False, "ok")]
    assert spy.calls == []  # the bound tripped before any judge call


async def test_cancellederror_in_a_source_propagates(monkeypatch):
    # AC7: `asyncio.CancelledError` raised in a source is genuine cancellation (a BaseException,
    # distinct from the TimeoutError the wall raises) and must PROPAGATE — never degraded to `ok`.
    async def _cancel_fetch(url, *, client=None, timeout):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cit, "_fetch_source", _cancel_fetch)
    spy = _CitationJudgeSpy()
    with pytest.raises(asyncio.CancelledError):
        await check_citations("out", [{"url": "https://x.example"}], "quick", provider=spy)


async def test_fetch_source_oversize_content_length_is_ambiguous():
    # AC7: a response whose body exceeds the byte cap → `ambiguous` (we cannot fairly judge a
    # truncated giant), no full-body judge. httpx sets content-length from the body automatically.
    big = b"x" * (_MAX_FETCH_BYTES + 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("https://big.example", client=client, timeout=1.0)
    assert result == ("ambiguous", "")  # oversize → ambiguous, body NOT handed to the judge


async def test_oversize_source_end_to_end_is_ok_not_fabricated():
    # AC7 end-to-end: an oversize source degrades to a conservative `ok` (ambiguous → ok), never a
    # false `fabricated`, and never spends a judge call on a multi-MB body.
    spy = _CitationJudgeSpy()
    big = b"x" * (_MAX_FETCH_BYTES + 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "the output",
            [{"url": "https://big.example"}],
            "quick",
            provider=spy,
            client=client,
        )
    assert result == [CitationCheck("https://big.example", False, False, "ok")]
    assert spy.calls == []  # no judge spend on the oversize body


# ============================================================ Story 3.4: provided-source cap (deferred 2.4)


def test_resolve_max_sources_hardening():
    # Default 50; a valid >0 int honoured; non-int / ≤0 → default (a 0 cap would drop ALL).
    assert _resolve_max_sources(None) == _DEFAULT_MAX_SOURCES
    assert _resolve_max_sources("10") == 10
    assert _resolve_max_sources("0") == _DEFAULT_MAX_SOURCES
    assert _resolve_max_sources("-3") == _DEFAULT_MAX_SOURCES
    assert _resolve_max_sources("nonsense") == _DEFAULT_MAX_SOURCES
    assert _resolve_max_sources("3.5") == _DEFAULT_MAX_SOURCES  # non-int string → default


async def test_provided_sources_capped_to_max_sources(monkeypatch):
    # A buyer submitting more provided sources than the cap → at most `PROOV_MAX_SOURCES` are
    # fetched AND judged (the paid-call amplification bound). First-seen order is preserved.
    monkeypatch.setenv("PROOV_MAX_SOURCES", "3")
    spy = _CitationJudgeSpy()
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text=_html("supporting text"))

    sources = [{"url": f"https://s{i}.example"} for i in range(5)]
    async with _mock_fetch_client(handler) as client:
        result = await check_citations("out", sources, "quick", provider=spy, client=client)

    assert len(fetched) == 3  # only the first cap sources are fetched (bounded outbound)
    assert len(spy.calls) == 3  # only the first cap sources are judged (bounded paid calls)
    assert [cc.source for cc in result] == [f"https://s{i}.example" for i in range(3)]


async def test_source_cap_does_not_limit_deep_discovered(monkeypatch):
    # The cap is on the PROVIDED (spend) surface only; Deep `discovered` appends are zero-cost
    # (already-judged, no re-fetch/re-judge) and must be unaffected by the cap.
    monkeypatch.setenv("PROOV_MAX_SOURCES", "1")
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("text"))

    provided = [{"url": "https://p1.example"}, {"url": "https://p2.example"}]
    discovered = [("https://d1.example", "supports"), ("https://d2.example", "neutral")]
    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "out", provided, "deep", provider=spy, client=client, discovered=discovered
        )

    provided_results = [cc for cc in result if cc.source.startswith("https://p")]
    discovered_results = [cc for cc in result if cc.source.startswith("https://d")]
    assert len(provided_results) == 1  # provided truncated to the cap
    assert len(discovered_results) == 2  # discovered untouched by the cap


async def test_source_cap_logs_truncation_warning(monkeypatch, caplog):
    monkeypatch.setenv("PROOV_MAX_SOURCES", "2")
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("text"))

    sources = [{"url": f"https://s{i}.example"} for i in range(5)]
    async with _mock_fetch_client(handler) as client:
        with caplog.at_level("WARNING"):
            await check_citations("out", sources, "quick", provider=spy, client=client)

    assert any("PROOV_MAX_SOURCES" in r.message for r in caplog.records)  # one warning, dropped count


# ----------------------------- Story 3.4 code-review patch P1: per-order cost-budget source cap


async def test_max_paid_sources_caps_the_paid_loop(monkeypatch):
    # P1: the engine passes how many provided sources the REMAINING per-order budget can afford;
    # check_citations truncates the paid fetch+judge loop to that bound so the citation check can
    # never push the order past its cost ceiling. A high PROOV_MAX_SOURCES proves the BUDGET is the
    # binding constraint here, not the amplification cap.
    monkeypatch.setenv("PROOV_MAX_SOURCES", "50")
    spy = _CitationJudgeSpy()
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text=_html("supporting text"))

    sources = [{"url": f"https://s{i}.example"} for i in range(5)]
    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "out", sources, "quick", provider=spy, client=client, max_paid_sources=2
        )

    assert len(fetched) == 2  # only the budget-affordable sources are fetched (bounded outbound)
    assert len(spy.calls) == 2  # only the budget-affordable sources are judged (bounded paid spend)
    assert [cc.source for cc in result] == [f"https://s{i}.example" for i in range(2)]


async def test_max_paid_sources_none_leaves_check_unbounded(monkeypatch):
    # P1: max_paid_sources=None (the disabled-meter / $0 default path) imposes no budget cap — every
    # provided source within PROOV_MAX_SOURCES is processed, byte-for-byte as before the patch.
    monkeypatch.setenv("PROOV_MAX_SOURCES", "50")
    spy = _CitationJudgeSpy()
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text=_html("supporting text"))

    sources = [{"url": f"https://s{i}.example"} for i in range(4)]
    async with _mock_fetch_client(handler) as client:
        result = await check_citations(
            "out", sources, "quick", provider=spy, client=client, max_paid_sources=None
        )

    assert len(fetched) == 4  # unbounded by budget
    assert len(result) == 4


async def test_max_paid_sources_does_not_limit_deep_discovered(monkeypatch, caplog):
    # P1: the budget cap is on the PROVIDED (paid) surface only and logs one warning when it bites;
    # Deep `discovered` appends are zero-cost (already-judged, no re-fetch/re-judge) and unaffected.
    monkeypatch.setenv("PROOV_MAX_SOURCES", "50")
    spy = _CitationJudgeSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("text"))

    provided = [{"url": f"https://p{i}.example"} for i in range(3)]
    discovered = [("https://d1.example", "supports"), ("https://d2.example", "neutral")]
    async with _mock_fetch_client(handler) as client:
        with caplog.at_level("WARNING"):
            result = await check_citations(
                "out", provided, "deep", provider=spy, client=client,
                discovered=discovered, max_paid_sources=1,
            )

    provided_results = [cc for cc in result if cc.source.startswith("https://p")]
    discovered_results = [cc for cc in result if cc.source.startswith("https://d")]
    assert len(provided_results) == 1  # provided truncated to the affordable budget (1 paid source)
    assert len(discovered_results) == 2  # discovered untouched by the budget cap
    assert any("cost ceiling" in r.message for r in caplog.records)  # one budget-cap warning


# --------------------------------------------------------------------------- SSRF guard (Story 4.4)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC-1918
        "172.16.0.1",  # RFC-1918
        "192.168.1.1",  # RFC-1918
        "169.254.169.254",  # cloud metadata (link-local)
        "::1",  # IPv6 loopback
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "0.0.0.0",  # unspecified
        "fe80::1",  # IPv6 link-local
        "not-an-ip",  # unparseable → blocked conservatively
    ],
)
def test_is_blocked_address_blocks_internal_and_metadata(ip):
    assert _is_blocked_address(ip) is True


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_is_blocked_address_allows_public(ip):
    assert _is_blocked_address(ip) is False


def test_ssrf_block_reason_rejects_non_http_scheme(monkeypatch):
    # A non-http(s) scheme is refused before any DNS resolution.
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34"])
    assert _ssrf_block_reason("file:///etc/passwd") is not None
    assert _ssrf_block_reason("gopher://example.com/") is not None
    assert _ssrf_block_reason("ftp://example.com/") is not None


def test_ssrf_block_reason_blocks_host_resolving_internal(monkeypatch):
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["127.0.0.1"])
    assert _ssrf_block_reason("http://localhost/") is not None


def test_ssrf_block_reason_blocks_when_any_record_internal(monkeypatch):
    # DNS-rebinding defence: a host with even ONE internal record is refused.
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34", "169.254.169.254"])
    assert _ssrf_block_reason("http://rebind.example/") is not None


def test_ssrf_block_reason_allows_public(monkeypatch):
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34"])
    assert _ssrf_block_reason("https://example.com/page") is None


def test_ssrf_block_reason_resolution_failure_is_not_a_block(monkeypatch):
    # A host that won't resolve is NOT proof of an internal target → not blocked here (the real
    # fetch surfaces it as the ambiguous transport outcome).
    monkeypatch.setattr(cit, "_resolve_host", lambda host: [])
    assert _ssrf_block_reason("http://nxdomain.invalid/") is None


async def test_fetch_source_blocks_loopback_to_ambiguous(monkeypatch):
    # A loopback target degrades to ambiguous, NEVER fabricated, with no socket call attempted.
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["127.0.0.1"])

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("blocked URL must not be fetched")

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("http://127.0.0.1/admin", client=client, timeout=1.0)
    assert result == ("ambiguous", "")


async def test_fetch_source_blocks_metadata_to_ambiguous(monkeypatch):
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["169.254.169.254"])

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("metadata URL must not be fetched")

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source(
            "http://169.254.169.254/latest/meta-data/", client=client, timeout=1.0
        )
    assert result == ("ambiguous", "")


async def test_fetch_source_blocks_non_http_scheme_to_ambiguous(monkeypatch):
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("non-http scheme must not be fetched")

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("file:///etc/passwd", client=client, timeout=1.0)
    assert result == ("ambiguous", "")


async def test_fetch_source_blocks_redirect_to_internal(monkeypatch):
    # The public entry URL is allowed, but it 302s to an internal host → blocked on the hop,
    # never followed to the metadata service.
    def resolver(host: str) -> list[str]:
        return ["169.254.169.254"] if host == "evil.internal" else ["93.184.216.34"]

    monkeypatch.setattr(cit, "_resolve_host", resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.internal":  # pragma: no cover - must not be reached
            raise AssertionError("redirect to internal must not be fetched")
        return httpx.Response(302, headers={"Location": "http://evil.internal/meta"})

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source(
            "https://public.example/start", client=client, timeout=1.0
        )
    assert result == ("ambiguous", "")


async def test_blocked_source_yields_ambiguous_ok_not_fabricated(monkeypatch):
    # The honesty invariant: a blocked fetch flows through check_citations to a non-fabricating
    # `ok` flag — refusing to reach a URL is NOT evidence it is fabricated.
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["127.0.0.1"])
    results = await check_citations(
        "Some output.",
        [{"url": "http://127.0.0.1/secret"}],
        "quick",
        provider=StubLLMProvider(),
    )
    assert len(results) == 1
    assert results[0].flag == "ok"
    assert results[0].flag != "fabricated"


async def test_normal_public_url_still_fetches_retrievable(monkeypatch):
    # The guard does not break the happy path: a public URL still fetches and judges.
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("Paris is the capital of France."))

    async with _mock_fetch_client(handler) as client:
        cls, text = await _fetch_source("https://example.com", client=client, timeout=1.0)
    assert cls == "retrievable"
    assert text == "Paris is the capital of France."


async def test_fetch_source_redirect_without_location_is_ambiguous(monkeypatch):
    # A 3xx with no/invalid Location (next_request is None) cannot be followed; it must degrade to
    # the conservative `ambiguous`, not be classified `retrievable` because its status is < 400.
    monkeypatch.setattr(cit, "_resolve_host", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)  # redirect status, but no Location header

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("https://example.com/moved", client=client, timeout=1.0)
    assert result == ("ambiguous", "")

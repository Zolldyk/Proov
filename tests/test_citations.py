"""Tests for the citation-check layer (`proov/citations.py`) — OFFLINE ONLY.

No test opens a real socket or calls Gemini: `_fetch_source` is driven by an injected
`httpx.MockTransport` returning canned responses, and the support judgment uses the offline
`StubLLMProvider` (deterministic `ok`) plus a small `_CitationJudgeSpy` for the
`unsupported`/`unverifiable`/no-call branches. No keys anywhere on the fetch path — these are
arbitrary buyer URLs. Harness mirrors `tests/test_search.py` / `tests/test_llm.py`.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from proov.citations import (
    _MAX_FETCH_CHARS,
    _DEFAULT_CITATION_TIMEOUT,
    _fetch_source,
    _flag_for,
    _resolve_timeout,
    _strip_html,
    check_citations,
)
from proov.llm import StubLLMProvider
from proov.types import CitationCheck, Claim, Evidence, Judgment


@pytest.fixture(autouse=True)
def _clear_citation_env(monkeypatch):
    """Citation env must not leak between tests (the entrypoint reads it directly)."""
    monkeypatch.delenv("PROOV_CITATION_TIMEOUT", raising=False)
    monkeypatch.delenv("PROOV_LLM_PROVIDER", raising=False)


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


# --------------------------------------------------------------------------- _fetch_source


async def test_fetch_source_200_returns_stripped_bounded_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_html("Paris is the capital of France."))

    async with _mock_fetch_client(handler) as client:
        retrievable, text = await _fetch_source(
            "https://example.com", client=client, timeout=1.0
        )
    assert retrievable is True
    assert text == "Paris is the capital of France."
    assert "<" not in text  # tags stripped


async def test_fetch_source_bounds_to_max_fetch_chars():
    long_body = "x" * (_MAX_FETCH_CHARS + 500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=long_body)

    async with _mock_fetch_client(handler) as client:
        retrievable, text = await _fetch_source(
            "https://example.com", client=client, timeout=1.0
        )
    assert retrievable is True
    assert len(text) == _MAX_FETCH_CHARS


@pytest.mark.parametrize("status", [404, 410, 500, 503])
async def test_fetch_source_4xx_5xx_is_not_retrievable(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    async with _mock_fetch_client(handler) as client:
        result = await _fetch_source("https://example.com", client=client, timeout=1.0)
    assert result == (False, "")


async def test_fetch_source_swallows_transport_errors():
    def connect_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns boom", request=request)

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    async with _mock_fetch_client(connect_handler) as client:
        assert await _fetch_source("https://a", client=client, timeout=1.0) == (False, "")
    async with _mock_fetch_client(timeout_handler) as client:
        assert await _fetch_source("https://b", client=client, timeout=1.0) == (False, "")


async def test_fetch_source_follows_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, text=_html("final page"))

    async with _mock_fetch_client(handler) as client:
        retrievable, text = await _fetch_source(
            "https://example.com/old", client=client, timeout=1.0
        )
    assert retrievable is True
    assert text == "final page"


# --------------------------------------------------------------------------- _flag_for (all five AC5 rows)


def test_flag_for_all_rows():
    assert _flag_for(False, None) == (False, "fabricated")
    assert _flag_for(False, "supported") == (False, "fabricated")  # never judged when unret.
    assert _flag_for(True, "supported") == (True, "ok")
    assert _flag_for(True, "unsupported") == (False, "misattributed")
    assert _flag_for(True, "unverifiable") == (False, "ok")
    assert _flag_for(True, None) == (False, "ok")


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

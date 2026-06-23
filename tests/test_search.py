"""Tests for the search provider layer (`proov/search.py`) — OFFLINE ONLY.

No test opens a real socket or calls Tavily/Wikipedia: `StubSearchProvider` covers the
offline path, and `WikipediaProvider`/`TavilyProvider` are driven by an injected
`httpx.MockTransport` returning canned JSON. Dummy keys only — never a real key, never
assert a key value. Harness mirrors `tests/test_llm.py`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from proov import redaction
from proov.search import (
    SearchError,
    SearchProvider,
    StubSearchProvider,
    TavilyProvider,
    WikipediaProvider,
    _MAX_SNIPPET_CHARS,
    _normalise_evidence,
    _resolve_timeout,
    _strip_html,
    default_search_chain,
    get_search_provider,
    retrieve_evidence,
)
from proov.cache import NullCache, SqliteEvidenceCache
from proov.types import Evidence

# Realistic-length dummy — never a 1-2 char key (register_secret over-redacts short literals).
_DUMMY_KEY = "tvly-dummy-key-xyz-1234567890"


@pytest.fixture(autouse=True)
def _isolate_registered_secrets():
    """Snapshot/restore the global redaction secret set.

    `TavilyProvider.__init__` calls `register_secret(api_key)`, which writes to the
    module-global `_LITERAL_SECRETS`. Without isolation a dummy key registered here would
    persist and scrub matching substrings in unrelated tests (e.g. `tests/test_redaction.py`).
    """
    snapshot = set(redaction._LITERAL_SECRETS)
    yield
    redaction._LITERAL_SECRETS.clear()
    redaction._LITERAL_SECRETS.update(snapshot)


@pytest.fixture(autouse=True)
def _clear_search_env(monkeypatch):
    """Search env must not leak between tests (factory/chain read it directly)."""
    monkeypatch.delenv("PROOV_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PROOV_SEARCH_TIMEOUT", raising=False)


# --------------------------------------------------------------------------- helpers


def _mock_wikipedia(handler) -> WikipediaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WikipediaProvider(timeout=1.0, client=client)


def _mock_tavily(handler) -> TavilyProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TavilyProvider(api_key=_DUMMY_KEY, timeout=1.0, client=client)


class _SpyProvider:
    """Records calls; returns canned evidence."""

    def __init__(self, canned: list[Evidence]) -> None:
        self.canned = canned
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, k: int) -> list[Evidence]:
        self.calls.append((query, k))
        return list(self.canned)


class _FailProvider:
    """Always raises SearchError (a real call failure)."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, k: int) -> list[Evidence]:
        self.calls += 1
        raise SearchError("boom")


class _SlowProvider:
    """Sleeps past any tiny timeout so the chain must fall through."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, k: int) -> list[Evidence]:
        self.calls += 1
        await asyncio.sleep(10)
        return [Evidence(source="https://slow", title="t", snippet="never")]


# --------------------------------------------------------------------------- Stub


async def test_stub_is_deterministic_and_caps():
    stub = StubSearchProvider()
    first = await stub.search("the earth is round", 3)
    second = await stub.search("the earth is round", 3)
    assert first == second
    assert len(first) == 3
    assert all(e.source.startswith("https://stub.local/") for e in first)


async def test_stub_honours_cap():
    stub = StubSearchProvider()
    assert len(await stub.search("anything", 1)) == 1


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
async def test_stub_blank_returns_empty(blank):
    assert await StubSearchProvider().search(blank, 3) == []


# --------------------------------------------------------------------------- _strip_html


def test_strip_html_removes_searchmatch_tags():
    assert _strip_html('third <span class="searchmatch">planet</span>') == "third planet"
    assert _strip_html("plain") == "plain"
    assert _strip_html("a  <b>b</b>\n c") == "a b c"


# --------------------------------------------------------------------------- _normalise_evidence


def test_normalise_dedupes_by_source_first_seen():
    raw = [
        Evidence(source="https://x", title="first", snippet="a"),
        Evidence(source="https://x", title="dup", snippet="b"),
        Evidence(source="https://y", title="other", snippet="c"),
    ]
    out = _normalise_evidence(raw, 10)
    assert [e.source for e in out] == ["https://x", "https://y"]
    assert out[0].title == "first"  # first-seen wins


def test_normalise_drops_blank_source_or_snippet():
    raw = [
        Evidence(source="", title="t", snippet="text"),
        Evidence(source="https://x", title="t", snippet="   "),
        Evidence(source="https://y", title="t", snippet="ok"),
    ]
    out = _normalise_evidence(raw, 10)
    assert [e.source for e in out] == ["https://y"]


def test_normalise_truncates_snippet_and_caps():
    raw = [
        Evidence(source=f"https://x/{i}", title="t", snippet="z" * (_MAX_SNIPPET_CHARS + 50))
        for i in range(5)
    ]
    out = _normalise_evidence(raw, 2)
    assert len(out) == 2
    assert all(len(e.snippet) == _MAX_SNIPPET_CHARS for e in out)


def test_normalise_zero_cap_returns_empty():
    raw = [Evidence(source="https://x", title="t", snippet="a")]
    assert _normalise_evidence(raw, 0) == []
    assert _normalise_evidence(raw, -1) == []


# --------------------------------------------------------------------------- Wikipedia


async def test_wikipedia_parses_and_strips_html():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "pages": [
                    {
                        "key": "Earth",
                        "title": "Earth",
                        "excerpt": 'third <span class="searchmatch">planet</span>',
                    }
                ]
            },
        )

    provider = _mock_wikipedia(handler)
    ev = await provider.search("earth", 3)

    assert ev == [
        Evidence(
            source="https://en.wikipedia.org/wiki/Earth",
            title="Earth",
            snippet="third planet",
        )
    ]
    assert seen["method"] == "GET"
    assert "/w/rest.php/v1/search/page" in seen["url"]
    assert "q=earth" in seen["url"]
    assert "limit=3" in seen["url"]


async def test_wikipedia_falls_back_to_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"pages": [{"key": "K", "title": "T", "description": "desc text"}]}
        )

    ev = await _mock_wikipedia(handler).search("q", 3)
    assert ev[0].snippet == "desc text"


async def test_wikipedia_blank_query_makes_no_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called["n"] += 1
        return httpx.Response(200, json={"pages": []})

    assert await _mock_wikipedia(handler).search("   ", 3) == []
    assert called["n"] == 0


async def test_wikipedia_transport_error_raises_searcherror():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SearchError):
        await _mock_wikipedia(handler).search("q", 3)


async def test_wikipedia_http_500_raises_searcherror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with pytest.raises(SearchError):
        await _mock_wikipedia(handler).search("q", 3)


async def test_wikipedia_unparseable_200_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    assert await _mock_wikipedia(handler).search("q", 3) == []


# --------------------------------------------------------------------------- Tavily


async def test_tavily_parses_and_hides_key():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://x", "title": "X", "content": "c", "score": 0.9}
                ]
            },
        )

    provider = _mock_tavily(handler)
    ev = await provider.search("q", 3)

    assert ev == [Evidence(source="https://x", title="X", snippet="c", score=0.9)]
    # Key travels in the Authorization header — NEVER the URL or body.
    assert seen["headers"]["Authorization"] == f"Bearer {_DUMMY_KEY}"
    assert _DUMMY_KEY not in seen["url"]
    assert _DUMMY_KEY not in seen["body"]


async def test_tavily_sends_max_results_and_depth():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"results": []})

    await _mock_tavily(handler).search("q", 5)
    assert seen["body"]["max_results"] == 5
    assert seen["body"]["search_depth"] == "basic"
    assert seen["body"]["query"] == "q"


async def test_tavily_blank_query_makes_no_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    assert await _mock_tavily(handler).search("  ", 3) == []
    assert called["n"] == 0


async def test_tavily_transport_error_raises_searcherror():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SearchError):
        await _mock_tavily(handler).search("q", 3)


async def test_tavily_http_401_raises_searcherror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(SearchError):
        await _mock_tavily(handler).search("q", 3)


async def test_tavily_unparseable_200_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"no": "results"})

    assert await _mock_tavily(handler).search("q", 3) == []


# --------------------------------------------------------------------------- retrieve_evidence chain


async def test_retrieve_falls_through_failing_provider():
    fail = _FailProvider()
    spy = _SpyProvider([Evidence(source="https://x", title="t", snippet="hit")])
    ev = await retrieve_evidence("q", "quick", providers=[fail, spy])
    assert [e.source for e in ev] == ["https://x"]
    assert fail.calls == 1
    assert spy.calls == [("q", 3)]  # quick tier k


async def test_retrieve_all_fail_returns_empty_not_raise():
    ev = await retrieve_evidence("q", "deep", providers=[_FailProvider(), _FailProvider()])
    assert ev == []


async def test_retrieve_timeout_falls_through():
    slow = _SlowProvider()
    spy = _SpyProvider([Evidence(source="https://x", title="t", snippet="hit")])
    ev = await retrieve_evidence("q", "quick", providers=[slow, spy], timeout=0.01)
    assert [e.source for e in ev] == ["https://x"]
    assert slow.calls == 1


async def test_retrieve_stops_at_first_non_empty():
    first = _SpyProvider([Evidence(source="https://a", title="t", snippet="a")])
    second = _SpyProvider([Evidence(source="https://b", title="t", snippet="b")])
    ev = await retrieve_evidence("q", "quick", providers=[first, second])
    assert [e.source for e in ev] == ["https://a"]
    assert second.calls == []  # short-circuited — never reached


async def test_retrieve_empty_provider_falls_through():
    empty = _SpyProvider([])
    spy = _SpyProvider([Evidence(source="https://x", title="t", snippet="hit")])
    ev = await retrieve_evidence("q", "quick", providers=[empty, spy])
    assert [e.source for e in ev] == ["https://x"]
    assert spy.calls == [("q", 3)]


async def test_retrieve_blank_query_returns_empty_no_provider_call():
    spy = _SpyProvider([Evidence(source="https://x", title="t", snippet="hit")])
    assert await retrieve_evidence("   ", "quick", providers=[spy]) == []
    assert spy.calls == []


async def test_retrieve_dedupes_and_caps_provider_output():
    dup = _SpyProvider(
        [
            Evidence(source="https://x", title="t", snippet="a"),
            Evidence(source="https://x", title="t", snippet="b"),
            Evidence(source="https://y", title="t", snippet="c"),
        ]
    )
    ev = await retrieve_evidence("q", "quick", providers=[dup])
    assert [e.source for e in ev] == ["https://x", "https://y"]


# --------------------------------------------------------------------------- Deep multi-source merge


async def test_deep_merges_across_all_providers():
    # Deep queries EVERY provider and returns the merged union (both providers consulted).
    first = _SpyProvider([Evidence(source="https://a", title="t", snippet="a")])
    second = _SpyProvider([Evidence(source="https://b", title="t", snippet="b")])
    ev = await retrieve_evidence("q", "deep", providers=[first, second])
    assert sorted(e.source for e in ev) == ["https://a", "https://b"]
    assert first.calls == [("q", 6)]  # deep tier k = DEEP_EVIDENCE_K
    assert second.calls == [("q", 6)]  # second IS consulted (unlike Quick)


async def test_deep_dedupes_merged_sources_first_seen():
    # A source returned by BOTH providers is listed once (first-seen wins, order preserved).
    first = _SpyProvider(
        [
            Evidence(source="https://dup", title="first", snippet="a"),
            Evidence(source="https://only-a", title="t", snippet="a2"),
        ]
    )
    second = _SpyProvider(
        [
            Evidence(source="https://dup", title="second", snippet="b"),
            Evidence(source="https://only-b", title="t", snippet="b2"),
        ]
    )
    ev = await retrieve_evidence("q", "deep", providers=[first, second])
    assert [e.source for e in ev] == [
        "https://dup",
        "https://only-a",
        "https://only-b",
    ]
    assert ev[0].title == "first"  # first-seen across the merge wins


async def test_deep_merge_caps_to_k():
    # The merged union is capped to the Deep k (6) even when the providers return more.
    first = _SpyProvider(
        [Evidence(source=f"https://a/{i}", title="t", snippet="a") for i in range(5)]
    )
    second = _SpyProvider(
        [Evidence(source=f"https://b/{i}", title="t", snippet="b") for i in range(5)]
    )
    ev = await retrieve_evidence("q", "deep", providers=[first, second])
    assert len(ev) == 6  # DEEP_EVIDENCE_K


async def test_deep_tolerates_failing_provider_in_chain():
    # A failing provider contributes nothing but does NOT abort the merge — the survivor's
    # results still come through.
    fail = _FailProvider()
    spy = _SpyProvider([Evidence(source="https://x", title="t", snippet="hit")])
    ev = await retrieve_evidence("q", "deep", providers=[fail, spy])
    assert [e.source for e in ev] == ["https://x"]
    assert fail.calls == 1
    assert spy.calls == [("q", 6)]


async def test_deep_all_empty_returns_empty():
    # Every provider yields nothing → [] (claim → unverifiable downstream).
    a = _SpyProvider([])
    b = _SpyProvider([])
    assert await retrieve_evidence("q", "deep", providers=[a, b]) == []


# --------------------------------------------------------------------------- cache wire-in (Story 2.8)


class _RaisingCache:
    """A cache whose `get` raises — proves the wire-in degrades to a live search, never raises."""

    async def get(self, key):
        raise RuntimeError("cache exploded")

    async def put(self, key, evidence):
        raise RuntimeError("cache exploded")


async def test_cache_hit_skips_search():
    # The core AC: a second identical call is served from cache with ZERO extra search calls.
    cache = SqliteEvidenceCache(":memory:")
    spy = _SpyProvider([Evidence(source="https://a", title="t", snippet="s")])
    first = await retrieve_evidence("q", "quick", providers=[spy], cache=cache)
    assert len(spy.calls) == 1
    second = await retrieve_evidence("q", "quick", providers=[spy], cache=cache)
    assert second == first
    assert len(spy.calls) == 1  # unchanged — served from cache, no new search call


async def test_cache_miss_on_different_tier_anti_poison():
    # A Quick entry must NOT be served to a Deep request (different result shape).
    cache = SqliteEvidenceCache(":memory:")
    spy = _SpyProvider([Evidence(source="https://a", title="t", snippet="s")])
    await retrieve_evidence("q", "quick", providers=[spy], cache=cache)
    assert len(spy.calls) == 1
    await retrieve_evidence("q", "deep", providers=[spy], cache=cache)
    assert len(spy.calls) == 2  # miss — the spy is called again


async def test_cache_miss_on_different_k_anti_poison():
    cache = SqliteEvidenceCache(":memory:")
    spy = _SpyProvider([Evidence(source="https://a", title="t", snippet="s")])
    await retrieve_evidence("q", "quick", providers=[spy], cache=cache, options={"k": 1})
    assert len(spy.calls) == 1
    await retrieve_evidence("q", "quick", providers=[spy], cache=cache, options={"k": 2})
    assert len(spy.calls) == 2  # different k → different key → miss


async def test_empty_result_is_not_cached():
    cache = SqliteEvidenceCache(":memory:")
    spy = _SpyProvider([])  # always empty
    await retrieve_evidence("q", "quick", providers=[spy], cache=cache)
    await retrieve_evidence("q", "quick", providers=[spy], cache=cache)
    assert len(spy.calls) == 2  # [] never cached — re-attempted every time


async def test_null_cache_behaves_as_uncached():
    spy = _SpyProvider([Evidence(source="https://a", title="t", snippet="s")])
    await retrieve_evidence("q", "quick", providers=[spy], cache=NullCache())
    await retrieve_evidence("q", "quick", providers=[spy], cache=NullCache())
    assert len(spy.calls) == 2  # every call hits the chain


async def test_cache_error_degrades_to_live_search():
    spy = _SpyProvider([Evidence(source="https://a", title="t", snippet="s")])
    # A get-raising cache must not crash the order — it degrades to a live search.
    ev = await retrieve_evidence("q", "quick", providers=[spy], cache=_RaisingCache())
    assert len(ev) == 1
    assert len(spy.calls) == 1


# --------------------------------------------------------------------------- conformance + factory/chain


def test_all_providers_conform_to_protocol():
    assert isinstance(StubSearchProvider(), SearchProvider)
    assert isinstance(WikipediaProvider(timeout=1.0), SearchProvider)
    assert isinstance(TavilyProvider(api_key=_DUMMY_KEY, timeout=1.0), SearchProvider)


def test_factory_resolves_stub_and_wikipedia():
    assert isinstance(get_search_provider("stub"), StubSearchProvider)
    assert isinstance(get_search_provider("wikipedia"), WikipediaProvider)


def test_factory_resolves_tavily_with_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", _DUMMY_KEY)
    assert isinstance(get_search_provider("tavily"), TavilyProvider)


def test_factory_tavily_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(SearchError):
        get_search_provider("tavily")


def test_factory_unknown_name_raises():
    with pytest.raises(SearchError):
        get_search_provider("nope")


def test_factory_honours_env_provider(monkeypatch):
    monkeypatch.setenv("PROOV_SEARCH_PROVIDER", "stub")
    assert isinstance(get_search_provider(), StubSearchProvider)


def test_chain_tavily_first_then_wikipedia_when_keyed(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", _DUMMY_KEY)
    chain = default_search_chain()
    assert [type(p) for p in chain] == [TavilyProvider, WikipediaProvider]


def test_chain_wikipedia_only_when_no_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    chain = default_search_chain()
    assert [type(p) for p in chain] == [WikipediaProvider]


def test_chain_forced_single_provider(monkeypatch):
    monkeypatch.setenv("PROOV_SEARCH_PROVIDER", "stub")
    chain = default_search_chain()
    assert [type(p) for p in chain] == [StubSearchProvider]


# --------------------------------------------------------------------------- _resolve_timeout


def test_resolve_timeout_rejects_non_finite_and_nonpositive():
    assert _resolve_timeout("inf") == 10.0
    assert _resolve_timeout("nan") == 10.0
    assert _resolve_timeout("-5") == 10.0
    assert _resolve_timeout("0") == 10.0
    assert _resolve_timeout("garbage") == 10.0
    assert _resolve_timeout(None) == 10.0
    assert _resolve_timeout("7.5") == 7.5

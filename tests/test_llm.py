"""Tests for the LLM provider layer (`proov/llm.py`) — OFFLINE ONLY.

No test opens a real socket or calls Gemini: `StubLLMProvider` covers the offline path,
and `GeminiProvider` is driven by an injected `httpx.MockTransport` returning canned
Gemini JSON. Dummy keys only — never a real key, never assert a key value.
"""

from __future__ import annotations

import json

import httpx
import pytest

from proov.llm import (
    GeminiProvider,
    LLMError,
    LLMProvider,
    StubLLMProvider,
    _normalise_claims,
    _resolve_timeout,
    extract_claims,
    get_llm_provider,
)
from proov import redaction
from proov.types import Claim

_DUMMY_KEY = "dummy-gemini-key-xyz"


@pytest.fixture(autouse=True)
def _isolate_registered_secrets():
    """Snapshot/restore the global redaction secret set.

    `GeminiProvider.__init__` calls `register_secret(api_key)`, which writes to the
    module-global `_LITERAL_SECRETS`. Without isolation a dummy key registered here would
    persist and scrub matching substrings in unrelated tests (e.g. `tests/test_redaction.py`).
    """
    snapshot = set(redaction._LITERAL_SECRETS)
    yield
    redaction._LITERAL_SECRETS.clear()
    redaction._LITERAL_SECRETS.update(snapshot)


# --------------------------------------------------------------------------- helpers


def _gemini_body(claims: list[str]) -> dict:
    """A canned Gemini 200 body carrying `claims` as the JSON-string candidate text."""
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(claims)}]}}
        ]
    }


def _mock_gemini(handler) -> GeminiProvider:
    """A GeminiProvider wired to an injected MockTransport (no real socket)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiProvider(api_key=_DUMMY_KEY, model="m", timeout=1.0, client=client)


class _SpyProvider:
    """Records whether/how `extract_claims` was called; returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def extract_claims(self, text: str, max_claims: int) -> list[Claim]:
        self.calls.append((text, max_claims))
        return [Claim(id="c1", text="from-provider")]


# --------------------------------------------------------------------------- Stub


async def test_stub_is_deterministic_and_splits_sentences():
    stub = StubLLMProvider()
    text = "Paris is the capital of France. The Eiffel Tower is in Paris."
    first = await stub.extract_claims(text, 20)
    second = await stub.extract_claims(text, 20)
    assert first == second
    assert [c.text for c in first] == [
        "Paris is the capital of France",
        "The Eiffel Tower is in Paris",
    ]
    assert [c.id for c in first] == ["c1", "c2"]


async def test_stub_honours_cap():
    stub = StubLLMProvider()
    text = "A. B. C. D. E."
    claims = await stub.extract_claims(text, 2)
    assert len(claims) == 2
    assert [c.text for c in claims] == ["A", "B"]


async def test_stub_case_insensitive_dedupe():
    stub = StubLLMProvider()
    claims = await stub.extract_claims("Paris is nice. PARIS IS NICE. Lyon too.", 20)
    assert [c.text for c in claims] == ["Paris is nice", "Lyon too"]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
async def test_stub_blank_returns_empty(blank):
    stub = StubLLMProvider()
    assert await stub.extract_claims(blank, 20) == []


# --------------------------------------------------------------------------- Gemini happy path


async def test_gemini_parses_dedupes_and_assigns_ids():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_gemini_body(["A", "B", "A"]))

    provider = _mock_gemini(handler)
    claims = await provider.extract_claims("some text", 20)

    assert claims == [Claim(id="c1", text="A"), Claim(id="c2", text="B")]
    # Key travels in the header, NEVER the URL query.
    assert seen["headers"]["x-goog-api-key"] == _DUMMY_KEY
    assert "key=" not in seen["url"]


async def test_gemini_honours_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_gemini_body(["A", "B", "C", "D"]))

    provider = _mock_gemini(handler)
    claims = await provider.extract_claims("text", 2)
    assert [c.text for c in claims] == ["A", "B"]


async def test_gemini_blank_input_makes_no_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called["n"] += 1
        return httpx.Response(200, json=_gemini_body(["A"]))

    provider = _mock_gemini(handler)
    assert await provider.extract_claims("   ", 20) == []
    assert called["n"] == 0


# --------------------------------------------------------------------------- Gemini failure split (AC6)


async def test_gemini_transport_error_raises_llmerror():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = _mock_gemini(handler)
    with pytest.raises(LLMError):
        await provider.extract_claims("text", 20)


async def test_gemini_http_500_raises_llmerror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    provider = _mock_gemini(handler)
    with pytest.raises(LLMError):
        await provider.extract_claims("text", 20)


async def test_gemini_unparseable_200_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})

    provider = _mock_gemini(handler)
    assert await provider.extract_claims("text", 20) == []


async def test_gemini_no_candidates_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    provider = _mock_gemini(handler)
    assert await provider.extract_claims("text", 20) == []


async def test_gemini_candidate_text_not_a_list_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "\"a string\""}]}}]})

    provider = _mock_gemini(handler)
    assert await provider.extract_claims("text", 20) == []


# --------------------------------------------------------------------------- top-level extract_claims


async def test_explicit_claims_bypass_provider():
    spy = _SpyProvider()
    claims = await extract_claims(
        "ignored output", "quick", provider=spy, explicit_claims=["x", "x", "y"]
    )
    assert claims == [Claim(id="c1", text="x"), Claim(id="c2", text="y")]
    assert spy.calls == []  # provider was NEVER called


async def test_empty_explicit_claims_falls_through_to_provider():
    spy = _SpyProvider()
    claims = await extract_claims(
        "real output", "deep", provider=spy, explicit_claims=[]
    )
    assert claims == [Claim(id="c1", text="from-provider")]
    # Provider called with the deep tier cap (50).
    assert spy.calls == [("real output", 50)]


async def test_extract_claims_passes_lowered_cap_to_provider():
    spy = _SpyProvider()
    await extract_claims("out", "deep", provider=spy, options={"max_claims": 7})
    assert spy.calls == [("out", 7)]


async def test_all_blank_explicit_claims_falls_through_to_provider():
    # An all-whitespace explicit list normalises to [] — it must NOT silently swallow
    # extraction; fall through to the provider with the tier cap.
    spy = _SpyProvider()
    claims = await extract_claims(
        "real output", "quick", provider=spy, explicit_claims=["   ", "\t\n"]
    )
    assert claims == [Claim(id="c1", text="from-provider")]
    assert spy.calls == [("real output", 20)]


# --------------------------------------------------------------------------- helpers (internal)


def test_normalise_claims_zero_cap_returns_empty():
    assert _normalise_claims(["a", "b"], 0) == []
    assert _normalise_claims(["a", "b"], -1) == []


def test_resolve_timeout_rejects_non_finite_and_nonpositive():
    assert _resolve_timeout("inf") == 30.0
    assert _resolve_timeout("nan") == 30.0
    assert _resolve_timeout("-5") == 30.0
    assert _resolve_timeout("0") == 30.0
    assert _resolve_timeout("garbage") == 30.0
    assert _resolve_timeout(None) == 30.0
    assert _resolve_timeout("12.5") == 12.5


# --------------------------------------------------------------------------- conformance + factory


def test_both_providers_conform_to_protocol():
    assert isinstance(StubLLMProvider(), LLMProvider)
    assert isinstance(
        GeminiProvider(api_key=_DUMMY_KEY, model="m", timeout=1.0), LLMProvider
    )


def test_factory_resolves_stub():
    assert isinstance(get_llm_provider("stub"), StubLLMProvider)


def test_factory_resolves_gemini_with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", _DUMMY_KEY)
    monkeypatch.delenv("PROOV_LLM_PROVIDER", raising=False)
    provider = get_llm_provider("gemini")
    assert isinstance(provider, GeminiProvider)


def test_factory_gemini_falls_back_to_google_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", _DUMMY_KEY)
    assert isinstance(get_llm_provider("gemini"), GeminiProvider)


def test_factory_missing_key_raises_llmerror(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError):
        get_llm_provider("gemini")


def test_factory_unknown_name_raises_llmerror():
    with pytest.raises(LLMError):
        get_llm_provider("nope")


def test_factory_honours_env_provider(monkeypatch):
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "stub")
    assert isinstance(get_llm_provider(), StubLLMProvider)


def test_factory_default_is_gemini(monkeypatch):
    monkeypatch.delenv("PROOV_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", _DUMMY_KEY)
    assert isinstance(get_llm_provider(), GeminiProvider)

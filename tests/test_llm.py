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
    _MAX_QUOTE_CHARS,
    _normalise_claims,
    _normalise_judgment,
    _resolve_timeout,
    extract_claims,
    get_llm_provider,
    judge_claim,
)
from proov import redaction
from proov.types import Claim, Evidence, EvidenceStance, Judgment

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


def _judge_body(obj: dict) -> dict:
    """A canned Gemini 200 body carrying the judge JSON OBJECT as candidate text."""
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(obj)}]}}
        ]
    }


class _JudgeSpyProvider:
    """Records whether `judge_claim` was called; returns a canned result or raises.

    Used for the top-level `judge_claim` entrypoint tests (it is duck-typed in, never
    `isinstance`-checked — so it need not implement `extract_claims`).
    """

    def __init__(self, *, result: Judgment | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[Claim, list[Evidence]]] = []
        self._result = result if result is not None else Judgment("supported", 0.7, ())
        self._raises = raises

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment:
        self.calls.append((claim, list(evidence)))
        if self._raises is not None:
            raise self._raises
        return self._result


_CLAIM = Claim(id="c1", text="Paris is the capital of France")
_EV1 = Evidence(source="https://a", title="A", snippet="Paris is the capital of France.")
_EV2 = Evidence(source="https://b", title="B", snippet="France's capital is Paris.")


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
    # Both must satisfy the now-TWO-method Protocol (extract_claims + judge_claim).
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


# =========================================================================== JUDGE (Story 2.3)
# --------------------------------------------------------------------------- Stub judge


async def test_stub_judge_no_evidence_is_unverifiable():
    stub = StubLLMProvider()
    assert await stub.judge_claim(_CLAIM, []) == Judgment("unverifiable", 0.0, ())


async def test_stub_judge_is_deterministic_one_stance_per_evidence():
    stub = StubLLMProvider()
    first = await stub.judge_claim(_CLAIM, [_EV1, _EV2])
    second = await stub.judge_claim(_CLAIM, [_EV1, _EV2])
    assert first == second  # deterministic: same input → same output
    assert first.status == "supported"
    assert 0.0 <= first.confidence <= 1.0
    # one EvidenceStance per input evidence, source preserved, stance "supports".
    assert [es.source for es in first.evidence] == ["https://a", "https://b"]
    assert all(es.stance == "supports" for es in first.evidence)


async def test_stub_judge_bounds_quote_to_max():
    stub = StubLLMProvider()
    long_ev = Evidence(source="https://a", title="A", snippet="x" * (_MAX_QUOTE_CHARS + 50))
    judgment = await stub.judge_claim(_CLAIM, [long_ev])
    assert len(judgment.evidence[0].quote) == _MAX_QUOTE_CHARS


# --------------------------------------------------------------------------- Gemini judge happy path


async def test_gemini_judge_parses_object_into_judgment():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json=_judge_body(
                {
                    "status": "supported",
                    "confidence": 0.8,
                    "evidence": [
                        {"source": "https://a", "quote": "q", "stance": "supports"}
                    ],
                }
            ),
        )

    provider = _mock_gemini(handler)
    judgment = await provider.judge_claim(_CLAIM, [_EV1, _EV2])

    assert judgment == Judgment(
        "supported", 0.8, (EvidenceStance("https://a", "q", "supports"),)
    )
    # Key travels in the header, NEVER the URL query.
    assert seen["headers"]["x-goog-api-key"] == _DUMMY_KEY
    assert "key=" not in seen["url"]


async def test_gemini_judge_empty_evidence_makes_no_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called["n"] += 1
        return httpx.Response(200, json=_judge_body({"status": "supported", "confidence": 1.0}))

    provider = _mock_gemini(handler)
    assert await provider.judge_claim(_CLAIM, []) == Judgment("unverifiable", 0.0, ())
    assert called["n"] == 0


# --------------------------------------------------------------------------- Gemini judge failure split


async def test_gemini_judge_transport_error_raises_llmerror():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = _mock_gemini(handler)
    with pytest.raises(LLMError):
        await provider.judge_claim(_CLAIM, [_EV1])


async def test_gemini_judge_http_500_raises_llmerror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    provider = _mock_gemini(handler)
    with pytest.raises(LLMError):
        await provider.judge_claim(_CLAIM, [_EV1])


async def test_gemini_judge_unparseable_200_returns_unverifiable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})

    provider = _mock_gemini(handler)
    assert await provider.judge_claim(_CLAIM, [_EV1]) == Judgment("unverifiable", 0.0, ())


async def test_gemini_judge_shapeless_200_returns_unverifiable():
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 whose candidate text is a JSON array, not the expected object.
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]})

    provider = _mock_gemini(handler)
    assert await provider.judge_claim(_CLAIM, [_EV1]) == Judgment("unverifiable", 0.0, ())


# --------------------------------------------------------------------------- _normalise_judgment


def test_normalise_judgment_drops_fabricated_source():
    raw = {
        "status": "supported",
        "confidence": 0.9,
        "evidence": [
            {"source": "https://a", "quote": "real", "stance": "supports"},
            {"source": "https://invented", "quote": "fake", "stance": "supports"},
        ],
    }
    j = _normalise_judgment(raw, [_EV1, _EV2])
    assert [es.source for es in j.evidence] == ["https://a"]  # invented source dropped
    assert j.status == "supported"  # one grounded item survives → label stands


def test_normalise_judgment_coerces_unknown_stance_to_neutral():
    raw = {
        "status": "supported",
        "confidence": 0.5,
        "evidence": [{"source": "https://a", "quote": "q", "stance": "wibble"}],
    }
    j = _normalise_judgment(raw, [_EV1])
    assert j.evidence[0].stance == "neutral"


def test_normalise_judgment_truncates_long_quote():
    raw = {
        "status": "supported",
        "confidence": 0.5,
        "evidence": [{"source": "https://a", "quote": "x" * (_MAX_QUOTE_CHARS + 99), "stance": "supports"}],
    }
    j = _normalise_judgment(raw, [_EV1])
    assert len(j.evidence[0].quote) == _MAX_QUOTE_CHARS


def test_normalise_judgment_clamps_confidence():
    raw = {"status": "supported", "confidence": 5.0, "evidence": [{"source": "https://a", "quote": "q", "stance": "supports"}]}
    assert _normalise_judgment(raw, [_EV1]).confidence == 1.0


def test_normalise_judgment_drops_blank_source_or_quote():
    raw = {
        "status": "supported",
        "confidence": 0.5,
        "evidence": [
            {"source": "https://a", "quote": "   ", "stance": "supports"},
            {"source": "", "quote": "q", "stance": "supports"},
        ],
    }
    # both dropped → no grounded evidence → calibration downgrade.
    assert _normalise_judgment(raw, [_EV1]) == Judgment("unverifiable", 0.5, ())


def test_normalise_judgment_downgrades_label_with_no_grounded_evidence():
    raw = {
        "status": "supported",
        "confidence": 0.9,
        "evidence": [{"source": "https://invented", "quote": "fake", "stance": "supports"}],
    }
    j = _normalise_judgment(raw, [_EV1])
    assert j.status == "unverifiable"  # label-needs-evidence guard
    assert j.evidence == ()


def test_normalise_judgment_unknown_status_is_unverifiable():
    raw = {"status": "wibble", "confidence": 0.5, "evidence": []}
    assert _normalise_judgment(raw, [_EV1]).status == "unverifiable"


def test_normalise_judgment_unverifiable_keeps_grounded_evidence():
    # unverifiable is NOT subject to the downgrade guard — grounded evidence may still ride along.
    raw = {
        "status": "unverifiable",
        "confidence": 0.3,
        "evidence": [{"source": "https://a", "quote": "q", "stance": "neutral"}],
    }
    j = _normalise_judgment(raw, [_EV1])
    assert j.status == "unverifiable"
    assert j.evidence == (EvidenceStance("https://a", "q", "neutral"),)


def test_normalise_judgment_tolerates_non_list_evidence_field():
    # A parseable-200 dict whose `evidence` is a truthy non-list (Gemini responseSchema is
    # best-effort, not a hard guarantee) must NOT raise — the seam degrades, never crashes.
    # Regression: previously `for item in raw.get("evidence") or []` iterated the scalar.
    j = _normalise_judgment({"status": "supported", "confidence": 0.8, "evidence": 5}, [_EV1])
    assert j == Judgment("unverifiable", 0.8, ())  # no grounded evidence → downgraded, no raise


# --------------------------------------------------------------------------- top-level judge_claim (Story 2.3 / AC7)


async def test_top_level_judge_empty_evidence_short_circuits_no_call():
    spy = _JudgeSpyProvider()
    result = await judge_claim(_CLAIM, [], "quick", provider=spy)
    assert result == Judgment("unverifiable", 0.0, ())
    assert spy.calls == []  # provider NEVER called for thin evidence


async def test_top_level_judge_llmerror_degrades_to_unverifiable():
    spy = _JudgeSpyProvider(raises=LLMError("down"))
    result = await judge_claim(_CLAIM, [_EV1], "quick", provider=spy)
    assert result == Judgment("unverifiable", 0.0, ())  # never raises out


async def test_top_level_judge_passes_provider_result_through():
    canned = Judgment("supported", 0.6, (EvidenceStance("https://a", "q", "supports"),))
    spy = _JudgeSpyProvider(result=canned)
    result = await judge_claim(_CLAIM, [_EV1, _EV2], "deep", provider=spy)
    assert result == canned
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == _CLAIM

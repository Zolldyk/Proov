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
    LLMQuotaError,
    StubLLMProvider,
    _MAX_CONSENSUS_EVIDENCE,
    _MAX_DEEP_PASSES,
    _MAX_QUOTE_CHARS,
    _consensus_judgment,
    _normalise_claims,
    _normalise_judgment,
    _resolve_deep_passes,
    _resolve_timeout,
    default_llm_chain,
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


async def test_top_level_judge_quick_is_single_pass_passthrough():
    # Quick judgment delegates ONCE and passes the provider result straight through.
    canned = Judgment("supported", 0.6, (EvidenceStance("https://a", "q", "supports"),))
    spy = _JudgeSpyProvider(result=canned)
    result = await judge_claim(_CLAIM, [_EV1, _EV2], "quick", provider=spy)
    assert result == canned
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == _CLAIM


# ------------------------------------------------------- Deep multi-pass self-consistency (Story 2.7)


class _CountingJudgeProvider:
    """A fake judge provider whose `judge_claim` returns a scripted result per call.

    Drives Deep multi-pass: each call pops the next scripted `Judgment` (or raises the next
    scripted exception), so a test can vary statuses across the self-consistency passes.
    """

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls = 0

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment:
        item = self._results[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def test_resolve_deep_passes_hardening():
    # default 3; garbage / ≤0 → 3; valid honoured; over-cap clamped to _MAX_DEEP_PASSES.
    assert _resolve_deep_passes(None) == 3
    assert _resolve_deep_passes("5") == 5
    assert _resolve_deep_passes("nonsense") == 3
    assert _resolve_deep_passes("3.5") == 3  # non-int string → default
    assert _resolve_deep_passes("0") == 3
    assert _resolve_deep_passes("-2") == 3
    assert _resolve_deep_passes("999") == _MAX_DEEP_PASSES  # capped


def test_consensus_empty_is_unverifiable():
    assert _consensus_judgment([]) == Judgment("unverifiable", 0.0, ())


def test_consensus_unanimous_keeps_full_confidence():
    es = EvidenceStance("https://a", "q", "supports")
    passes = [Judgment("supported", 0.9, (es,)) for _ in range(3)]
    result = _consensus_judgment(passes)
    assert result.status == "supported"
    assert result.confidence == pytest.approx(0.9)  # agreement 3/3 → full mean
    assert result.evidence == (es,)


def test_consensus_majority_down_weights_confidence():
    # 2 supported (0.9 each) + 1 unsupported → supported, mean*agreement = 0.9 * (2/3).
    passes = [
        Judgment("supported", 0.9, (EvidenceStance("https://a", "q", "supports"),)),
        Judgment("supported", 0.9, (EvidenceStance("https://a", "q", "supports"),)),
        Judgment("unsupported", 0.8, (EvidenceStance("https://b", "r", "refutes"),)),
    ]
    result = _consensus_judgment(passes)
    assert result.status == "supported"
    assert result.confidence == pytest.approx(0.9 * (2 / 3))
    # Evidence is only the winning (supported) passes' grounded stances, deduped.
    assert result.evidence == (EvidenceStance("https://a", "q", "supports"),)


def test_consensus_three_way_split_is_unverifiable():
    passes = [
        Judgment("supported", 0.9, ()),
        Judgment("unsupported", 0.9, ()),
        Judgment("unverifiable", 0.9, ()),
    ]
    assert _consensus_judgment(passes) == Judgment("unverifiable", 0.0, ())


def test_consensus_even_tie_is_unverifiable():
    # 2 vs 2 at the top count → no unique plurality → unverifiable.
    passes = [
        Judgment("supported", 0.9, ()),
        Judgment("supported", 0.9, ()),
        Judgment("unsupported", 0.9, ()),
        Judgment("unsupported", 0.9, ()),
    ]
    assert _consensus_judgment(passes) == Judgment("unverifiable", 0.0, ())


def test_consensus_is_order_independent():
    import random

    passes = [
        Judgment("supported", 0.7, (EvidenceStance("https://a", "qa", "supports"),)),
        Judgment("supported", 0.9, (EvidenceStance("https://b", "qb", "supports"),)),
        Judgment("unsupported", 0.5, (EvidenceStance("https://c", "qc", "refutes"),)),
    ]
    baseline = _consensus_judgment(passes)
    for _ in range(5):
        shuffled = passes[:]
        random.shuffle(shuffled)
        assert _consensus_judgment(shuffled) == baseline


def test_consensus_caps_merged_evidence():
    # Winning passes carrying many distinct stances are bounded to _MAX_CONSENSUS_EVIDENCE.
    big = tuple(
        EvidenceStance(f"https://s/{i}", f"q{i}", "supports") for i in range(20)
    )
    passes = [Judgment("supported", 0.8, big) for _ in range(3)]
    result = _consensus_judgment(passes)
    assert len(result.evidence) == _MAX_CONSENSUS_EVIDENCE


async def test_deep_judge_majority_consensus():
    # 3 passes: supported, supported, unsupported → consensus supported (majority).
    es = EvidenceStance("https://a", "q", "supports")
    provider = _CountingJudgeProvider(
        [
            Judgment("supported", 0.9, (es,)),
            Judgment("supported", 0.9, (es,)),
            Judgment("unsupported", 0.8, (EvidenceStance("https://b", "r", "refutes"),)),
        ]
    )
    result = await judge_claim(_CLAIM, [_EV1, _EV2], "deep", provider=provider)
    assert provider.calls == 3  # multi-pass: sampled PROOV_DEEP_JUDGE_PASSES (default 3) times
    assert result.status == "supported"
    assert result.confidence == pytest.approx(0.9 * (2 / 3))


async def test_deep_judge_raising_pass_counts_as_unverifiable_vote():
    # A pass that raises LLMError becomes an unverifiable vote; never raises out. Here:
    # supported, LLMError(→unverifiable), unverifiable → unverifiable wins (2 of 3).
    provider = _CountingJudgeProvider(
        [
            Judgment("supported", 0.9, (EvidenceStance("https://a", "q", "supports"),)),
            LLMError("pass 2 down"),
            Judgment("unverifiable", 0.4, ()),
        ]
    )
    result = await judge_claim(_CLAIM, [_EV1], "deep", provider=provider)
    assert provider.calls == 3
    assert result.status == "unverifiable"


async def test_deep_judge_empty_evidence_short_circuits_no_passes():
    spy = _JudgeSpyProvider()
    result = await judge_claim(_CLAIM, [], "deep", provider=spy)
    assert result == Judgment("unverifiable", 0.0, ())
    assert spy.calls == []  # no multi-pass spend on nothing


# ============================================================ Story 3.4: quota-aware fallback chain


class _QuotaHead:
    """A fake `LLMProvider` head whose every call raises `LLMQuotaError` (a 429-routing head).

    Drives the chain-routing tests: a Gemini-shaped head that is quota-exhausted, so the
    entrypoint must fall through to the next provider (the $0 Stub tail) for every call.
    """

    model = "quota-head"

    def __init__(self) -> None:
        self.extract_calls = 0
        self.judge_calls = 0

    async def extract_claims(self, text: str, max_claims: int) -> list[Claim]:
        self.extract_calls += 1
        raise LLMQuotaError("simulated 429")

    async def judge_claim(self, claim: Claim, evidence: list[Evidence]) -> Judgment:
        self.judge_calls += 1
        raise LLMQuotaError("simulated 429")


# ------------------------------------------------------------ default_llm_chain shapes (AC1b)


def test_default_llm_chain_forced_single_when_provider_env_set(monkeypatch):
    # PROOV_LLM_PROVIDER forces a SINGLE provider — no surprise Stub tail appended.
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "stub")
    chain = default_llm_chain()
    assert len(chain) == 1
    assert isinstance(chain[0], StubLLMProvider)


def test_default_llm_chain_keyed_is_gemini_then_stub(monkeypatch):
    # A key present (and no forced provider) → Gemini-first, ALWAYS ending in the $0 Stub tail.
    monkeypatch.delenv("PROOV_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", _DUMMY_KEY)
    chain = default_llm_chain()
    assert len(chain) == 2
    assert isinstance(chain[0], GeminiProvider)
    assert isinstance(chain[1], StubLLMProvider)  # keyless $0 tail (LLM analogue of Wikipedia)


def test_default_llm_chain_unkeyed_is_stub_only(monkeypatch):
    # No forced provider and no key → the chain is just the always-available $0 Stub (never empty).
    monkeypatch.delenv("PROOV_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    chain = default_llm_chain()
    assert len(chain) == 1
    assert isinstance(chain[0], StubLLMProvider)


# ------------------------------------------------------------ GeminiProvider 429 → LLMQuotaError (AC1a)


async def test_gemini_extract_429_raises_quota_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    provider = _mock_gemini(handler)
    with pytest.raises(LLMQuotaError):
        await provider.extract_claims("some text", 20)


async def test_gemini_judge_429_raises_quota_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    provider = _mock_gemini(handler)
    with pytest.raises(LLMQuotaError):
        await provider.judge_claim(_CLAIM, [_EV1])


async def test_gemini_non_429_status_is_plain_llmerror_not_quota():
    # A 500 is a generic failure, NOT a quota signal — it must stay a plain LLMError so it is
    # not mistaken for a routable 429 (it routes too, but the type discriminates the cause).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    provider = _mock_gemini(handler)
    with pytest.raises(LLMError) as exc_info:
        await provider.extract_claims("some text", 20)
    assert not isinstance(exc_info.value, LLMQuotaError)


# ------------------------------------------------------------ entrypoint chain routing (AC1c)


async def test_extract_claims_routes_past_quota_head_to_stub_tail():
    # A keyed-but-quota-exhausted order: the head 429s, the chain falls through to the $0 Stub
    # tail, which serves the extraction — the order's claims are preserved, not dropped.
    head = _QuotaHead()
    chain = [head, StubLLMProvider()]
    claims = await extract_claims("Paris is the capital of France.", "quick", providers=chain)
    assert head.extract_calls == 1  # head WAS tried first
    assert [c.text for c in claims] == ["Paris is the capital of France"]  # Stub tail served


async def test_extract_claims_all_providers_fail_reraises_last_error():
    # A forced single non-Stub chain that fully fails must re-raise so the engine degrades to
    # zero claims (→ partial). Two quota heads, no Stub tail → the last LLMError propagates.
    chain = [_QuotaHead(), _QuotaHead()]
    with pytest.raises(LLMError):
        await extract_claims("some text", "quick", providers=chain)


async def test_extract_claims_empty_chain_raises_llmerror():
    # An empty chain (the engine's degraded `chain = []`) raises so extraction → zero claims.
    with pytest.raises(LLMError):
        await extract_claims("some text", "quick", providers=[])


async def test_judge_claim_quick_routes_past_quota_head_to_stub_tail():
    head = _QuotaHead()
    chain = [head, StubLLMProvider()]
    result = await judge_claim(_CLAIM, [_EV1], "quick", providers=chain)
    assert head.judge_calls == 1  # head tried first, 429'd
    assert result.status == "supported"  # Stub tail judged (optimistic offline verdict)


async def test_judge_claim_quick_all_fail_degrades_to_unverifiable():
    # No Stub tail and every provider 429s → the entrypoint NEVER raises out; the claim degrades.
    chain = [_QuotaHead(), _QuotaHead()]
    result = await judge_claim(_CLAIM, [_EV1], "quick", providers=chain)
    assert result == Judgment("unverifiable", 0.0, ())


async def test_judge_claim_deep_falls_through_per_pass_to_stub_tail():
    # Each Deep self-consistency pass walks the chain: head 429s per pass → Stub tail answers.
    # 3 passes all "supported" → consensus supported. The head is hit once PER pass.
    head = _QuotaHead()
    chain = [head, StubLLMProvider()]
    result = await judge_claim(_CLAIM, [_EV1], "deep", providers=chain)
    assert head.judge_calls == _resolve_deep_passes()  # one fall-through per pass
    assert result.status == "supported"


async def test_provider_single_backcompat_unchanged():
    # The pre-3.4 `provider=`-single param still works (a one-element chain), so the existing
    # llm tests are untouched: a single spy provider serves the extraction with no chain.
    spy = _SpyProvider()
    claims = await extract_claims("text", "quick", provider=spy)
    assert claims == [Claim(id="c1", text="from-provider")]
    assert len(spy.calls) == 1

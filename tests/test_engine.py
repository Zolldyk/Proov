"""Tests for the `[B]` Verification Engine orchestrator (`proov/engine.py`).

Fully offline ($0, NFR1): no real Gemini/Tavily/Wikipedia, no sockets. The default fixture
forces the deterministic stub LLM + stub search providers via env; tests that need a precise
LLM behaviour (raise, unsupported, controlled clock) monkeypatch `engine.get_llm_provider`
or `engine.check_citations` / `engine._now`. The engine NEVER raises out (NFR3) — that
contract is exercised directly.
"""

from __future__ import annotations

import pytest

from proov import engine as engine_mod
from proov.engine import verify
from proov.llm import LLMError
from proov.types import (
    CitationCheck,
    Claim,
    EvidenceStance,
    Judgment,
    Report,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Force the deterministic offline stub providers so no test touches the network."""
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "stub")
    monkeypatch.setenv("PROOV_SEARCH_PROVIDER", "stub")
    # A generous default budget so the SLA early-stop never fires unless a test drives it.
    monkeypatch.delenv("PROOV_QUICK_SLA_SECONDS", raising=False)


class FakeLLMProvider:
    """An injectable `LLMProvider` with a known `model` and controllable behaviour."""

    model = "fake-model"

    def __init__(
        self,
        claim_texts=("Claim one.", "Claim two.", "Claim three."),
        *,
        judge_status="supported",
        judge_confidence=0.8,
        raise_on_extract=False,
        raise_on_judge=False,
    ) -> None:
        self.claim_texts = list(claim_texts)
        self.judge_status = judge_status
        self.judge_confidence = judge_confidence
        self.raise_on_extract = raise_on_extract
        self.raise_on_judge = raise_on_judge
        self.judge_calls = 0

    async def extract_claims(self, text, max_claims):
        if self.raise_on_extract:
            raise LLMError("extract boom")
        claims = [Claim(id=f"c{i}", text=t) for i, t in enumerate(self.claim_texts, 1)]
        return claims[:max_claims]

    async def judge_claim(self, claim, evidence):
        self.judge_calls += 1
        if self.raise_on_judge:
            raise LLMError("judge boom")
        grounded = tuple(
            EvidenceStance(source=e.source, quote=e.snippet[:50], stance="supports")
            for e in evidence
        )
        return Judgment(self.judge_status, self.judge_confidence, grounded)


class _FakeClock:
    """A monotonic-ish fake `_now`: returns each scripted value, clamping to the last."""

    def __init__(self, times) -> None:
        self._times = list(times)
        self.calls = 0

    def __call__(self) -> float:
        value = self._times[min(self.calls, len(self._times) - 1)]
        self.calls += 1
        return value


async def test_happy_multi_claim_quick_run_yields_consistent_report():
    # Two sentences → two claims; stub search supplies evidence → stub judge "supported".
    inp = {"output": "Paris is the capital of France. The Louvre is in Paris.", "mode": "quick"}
    report = await verify(inp, "quick")

    assert isinstance(report, Report)
    # Findings are in extraction order (c1, c2) and each carries its judgment.
    assert [f.claim.id for f in report.findings] == ["c1", "c2"]
    assert all(f.judgment.status == "supported" for f in report.findings)
    # verdict == aggregate over the findings: 2 supported, none unverifiable → pass.
    assert report.verdict.label == "pass"
    assert report.verdict.claims_total == 2
    assert report.verdict.supported == 2
    # Model is the active (stub) provider's id — the honest non-Gemini id.
    assert report.model == "stub-llm"


async def test_model_is_the_injected_providers_id(monkeypatch):
    fake = FakeLLMProvider()
    monkeypatch.setattr(engine_mod, "get_llm_provider", lambda *a, **k: fake)
    report = await verify({"output": "Anything here."}, "quick")
    assert report.model == "fake-model"


async def test_zero_claim_output_is_partial_never_pass():
    # Empty output → zero claims → aggregate over [] → partial (never pass).
    report = await verify({"output": "", "mode": "quick"}, "quick")
    assert report.verdict.label == "partial"
    assert report.verdict.label != "pass"
    assert report.findings == ()


async def test_unsupported_judgment_drives_verdict_to_fail(monkeypatch):
    fake = FakeLLMProvider(claim_texts=("A claim.",), judge_status="unsupported")
    monkeypatch.setattr(engine_mod, "get_llm_provider", lambda *a, **k: fake)
    report = await verify({"output": "A claim."}, "quick")
    # FR10: an unsupported (refuted) claim → fail.
    assert report.verdict.label == "fail"


async def test_fabricated_citation_drives_verdict_to_fail(monkeypatch):
    # A `sources` input flows through check_citations; a fabricated source → fail.
    fabricated = CitationCheck(
        source="https://made.up/nonexistent",
        retrievable=False,
        supports_attached_claim=False,
        flag="fabricated",
    )

    async def _fake_check(output, sources, tier, **kwargs):
        # Prove the engine threads the buyer-provided sources into the citation check.
        assert sources == [{"url": "https://made.up/nonexistent"}]
        return [fabricated]

    monkeypatch.setattr(engine_mod, "check_citations", _fake_check)
    inp = {
        "output": "Some text.",
        "sources": [{"url": "https://made.up/nonexistent"}],
    }
    report = await verify(inp, "quick")
    assert report.verdict.label == "fail"
    assert report.citations == (fabricated,)


async def test_sla_early_stop_before_any_claim_yields_partial_without_raising(monkeypatch):
    # Deadline already passed at the first iteration check → loop breaks before judging any
    # claim → zero completed findings → partial. No raise.
    monkeypatch.setattr(engine_mod, "_now", _FakeClock([0.0, 10_000.0]))  # start=0, check=10000
    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", lambda *a, **k: 240.0)
    inp = {"output": "One. Two. Three.", "mode": "quick"}
    report = await verify(inp, "quick")
    assert report.verdict.label == "partial"
    assert report.findings == ()  # stopped before judging anything


async def test_sla_early_stop_preserves_completed_findings(monkeypatch):
    # Clock: start=0; iter1 check=100 (<240, judge c1); iter2 check=300 (>=240, break).
    # → exactly ONE completed finding, aggregated into a real verdict, no raise.
    fake = FakeLLMProvider(
        claim_texts=("c1.", "c2.", "c3."), judge_status="unverifiable"
    )
    monkeypatch.setattr(engine_mod, "get_llm_provider", lambda *a, **k: fake)
    monkeypatch.setattr(engine_mod, "_now", _FakeClock([0.0, 100.0, 300.0]))
    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", lambda *a, **k: 240.0)
    report = await verify({"output": "c1. c2. c3."}, "quick")
    assert len(report.findings) == 1  # only the first claim was judged before the budget hit
    assert fake.judge_calls == 1
    # One unverifiable finding → partial (the honest early-stop outcome).
    assert report.verdict.label == "partial"


async def test_verify_never_raises_out_when_judge_provider_misbehaves(monkeypatch):
    # A provider whose judge raises LLMError: judge_claim degrades that claim to
    # unverifiable, so verify returns a real Report (partial) rather than raising.
    fake = FakeLLMProvider(claim_texts=("A.", "B."), raise_on_judge=True)
    monkeypatch.setattr(engine_mod, "get_llm_provider", lambda *a, **k: fake)
    report = await verify({"output": "A. B."}, "quick")
    assert report.verdict.label == "partial"
    assert all(f.judgment.status == "unverifiable" for f in report.findings)


async def test_verify_degrades_to_zero_claims_when_extraction_raises(monkeypatch):
    # extract_claims is the one entrypoint that raises out; the engine wraps ONLY it and
    # degrades to zero claims (→ partial), never raising.
    fake = FakeLLMProvider(raise_on_extract=True)
    monkeypatch.setattr(engine_mod, "get_llm_provider", lambda *a, **k: fake)
    report = await verify({"output": "irrelevant"}, "quick")
    assert report.findings == ()
    assert report.verdict.label == "partial"


async def test_verify_degrades_when_llm_provider_unresolvable(monkeypatch):
    # A config error resolving the provider must not crash a paid order. An unresolvable
    # provider fails BOTH the engine's resolution (→ "unknown-model") AND the entrypoint's
    # fallback resolution (→ extraction raises LLMError → zero claims → partial). No raise.
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "no-such-provider")
    report = await verify({"output": "Some output."}, "quick")
    assert report.model == "unknown-model"
    assert report.findings == ()
    assert report.verdict.label == "partial"


async def test_options_merge_call_level_wins(monkeypatch):
    # The engine merges input["options"] with call-level options (call-level wins). Capture
    # what reaches extract_claims to prove the merge.
    captured = {}

    async def _fake_extract(text, tier, *, provider=None, options=None, explicit_claims=None):
        captured["options"] = options
        return []

    monkeypatch.setattr(engine_mod, "extract_claims", _fake_extract)
    inp = {"output": "x", "options": {"max_claims": 5, "language": "en"}}
    await verify(inp, "quick", options={"max_claims": 2})
    assert captured["options"] == {"max_claims": 2, "language": "en"}


async def test_explicit_claims_bypass_is_threaded(monkeypatch):
    # The PRD §6 explicit-`claims` bypass must reach extract_claims.
    captured = {}

    async def _fake_extract(text, tier, *, provider=None, options=None, explicit_claims=None):
        captured["explicit_claims"] = explicit_claims
        return []

    monkeypatch.setattr(engine_mod, "extract_claims", _fake_extract)
    await verify({"output": "x", "claims": ["The sky is blue."]}, "quick")
    assert captured["explicit_claims"] == ["The sky is blue."]


def test_resolve_sla_seconds_hardening(monkeypatch):
    # Mirror the other timeout-var hardening: default 240; garbage / non-finite / ≤0 → 240.
    assert engine_mod._resolve_sla_seconds(None) == 240.0
    assert engine_mod._resolve_sla_seconds("120") == 120.0
    assert engine_mod._resolve_sla_seconds("nonsense") == 240.0
    assert engine_mod._resolve_sla_seconds("0") == 240.0
    assert engine_mod._resolve_sla_seconds("-5") == 240.0
    assert engine_mod._resolve_sla_seconds("inf") == 240.0

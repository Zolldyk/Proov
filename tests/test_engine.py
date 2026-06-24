"""Tests for the `[B]` Verification Engine orchestrator (`proov/engine.py`).

Fully offline ($0, NFR1): no real Gemini/Tavily/Wikipedia, no sockets. The default fixture
forces the deterministic stub LLM + stub search providers via env; tests that need a precise
LLM behaviour (raise, unsupported, controlled clock) monkeypatch `engine.default_llm_chain`
(returning a one-element `[fake]` chain) or `engine.check_citations` / `engine._now`. The
engine NEVER raises out (NFR3) — that contract is exercised directly.
"""

from __future__ import annotations

import asyncio

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
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
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
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
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
    # Per-slice granularity (Story 3.3): the clock is now read BETWEEN retrieve and judge, so the
    # scripted sequence is deadline=0; iter1 pre-retrieve=100 (<240, remaining 140), iter1
    # post-retrieve=110 (<240, remaining 130 → judge c1); iter2 pre-retrieve=300 (>=240, break).
    # → exactly ONE completed finding, aggregated into a real verdict, no raise.
    fake = FakeLLMProvider(
        claim_texts=("c1.", "c2.", "c3."), judge_status="unverifiable"
    )
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setattr(engine_mod, "_now", _FakeClock([0.0, 100.0, 110.0, 300.0]))
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
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    report = await verify({"output": "A. B."}, "quick")
    assert report.verdict.label == "partial"
    assert all(f.judgment.status == "unverifiable" for f in report.findings)


async def test_verify_degrades_to_zero_claims_when_extraction_raises(monkeypatch):
    # extract_claims is the one entrypoint that raises out; the engine wraps ONLY it and
    # degrades to zero claims (→ partial), never raising.
    fake = FakeLLMProvider(raise_on_extract=True)
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
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

    async def _fake_extract(
        text, tier, *, provider=None, providers=None, options=None, explicit_claims=None
    ):
        captured["options"] = options
        return []

    monkeypatch.setattr(engine_mod, "extract_claims", _fake_extract)
    inp = {"output": "x", "options": {"max_claims": 5, "language": "en"}}
    await verify(inp, "quick", options={"max_claims": 2})
    assert captured["options"] == {"max_claims": 2, "language": "en"}


async def test_explicit_claims_bypass_is_threaded(monkeypatch):
    # The PRD §6 explicit-`claims` bypass must reach extract_claims.
    captured = {}

    async def _fake_extract(
        text, tier, *, provider=None, providers=None, options=None, explicit_claims=None
    ):
        captured["explicit_claims"] = explicit_claims
        return []

    monkeypatch.setattr(engine_mod, "extract_claims", _fake_extract)
    await verify({"output": "x", "claims": ["The sky is blue."]}, "quick")
    assert captured["explicit_claims"] == ["The sky is blue."]


def test_resolve_sla_seconds_quick_hardening():
    # Quick tier: default 240; garbage / non-finite / ≤0 → 240; a valid value is honoured.
    assert engine_mod._resolve_sla_seconds("quick", None) == 240.0
    assert engine_mod._resolve_sla_seconds("quick", "120") == 120.0
    assert engine_mod._resolve_sla_seconds("quick", "nonsense") == 240.0
    assert engine_mod._resolve_sla_seconds("quick", "0") == 240.0
    assert engine_mod._resolve_sla_seconds("quick", "-5") == 240.0
    assert engine_mod._resolve_sla_seconds("quick", "inf") == 240.0


def test_resolve_sla_seconds_deep_hardening():
    # Deep tier: default 1680 (28 min); same garbage/non-finite/≤0 hardening as Quick.
    assert engine_mod._resolve_sla_seconds("deep", None) == 1680.0
    assert engine_mod._resolve_sla_seconds("deep", "900") == 900.0
    assert engine_mod._resolve_sla_seconds("deep", "nonsense") == 1680.0
    assert engine_mod._resolve_sla_seconds("deep", "0") == 1680.0
    assert engine_mod._resolve_sla_seconds("deep", "-5") == 1680.0
    assert engine_mod._resolve_sla_seconds("deep", "nan") == 1680.0


def test_resolve_sla_seconds_reads_tier_specific_env(monkeypatch):
    # Each tier reads its OWN env var; the other tier's var does not bleed across.
    monkeypatch.setenv("PROOV_QUICK_SLA_SECONDS", "111")
    monkeypatch.setenv("PROOV_DEEP_SLA_SECONDS", "2222")
    assert engine_mod._resolve_sla_seconds("quick") == 111.0
    assert engine_mod._resolve_sla_seconds("deep") == 2222.0


async def test_deep_run_uses_the_deep_sla_budget(monkeypatch):
    # A Deep run resolves the Deep budget (1680 default, here via the deep env var). With a
    # clock that never trips it, all claims are judged — the Deep budget is the one in force.
    captured = {}
    real_resolve = engine_mod._resolve_sla_seconds

    def _spy_resolve(tier, raw=None):
        captured["tier"] = tier
        return real_resolve(tier, raw)

    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", _spy_resolve)
    fake = FakeLLMProvider(claim_texts=("A.", "B."))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    report = await verify({"output": "A. B."}, "deep")
    assert captured["tier"] == "deep"
    assert len(report.findings) == 2  # generous Deep budget → both claims judged


async def test_deep_collects_discovered_sources_excluding_provided(monkeypatch):
    # A Deep run collects discovered (source, stance) from the findings' grounded evidence,
    # first-seen deduped and EXCLUDING any buyer-provided url, and passes them to
    # check_citations. The fake judge grounds each claim on the stub-search evidence sources.
    captured = {}

    fake = FakeLLMProvider(claim_texts=("A.", "B."), judge_status="supported")
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])

    async def _spy_check(output, sources, tier, **kwargs):
        captured["tier"] = tier
        captured["discovered"] = kwargs.get("discovered")
        return []

    monkeypatch.setattr(engine_mod, "check_citations", _spy_check)

    # The stub search provider yields sources like https://stub.local/<slug>/<i>. Provide one
    # of them as a buyer source so it is EXCLUDED from discovered.
    report = await verify(
        {"output": "A. B.", "sources": [{"url": "https://stub.local/a/1"}]}, "deep"
    )
    assert captured["tier"] == "deep"
    discovered = captured["discovered"]
    # Discovered is a list of (source, stance) tuples; the provided url is excluded; deduped.
    assert discovered is not None
    assert all(isinstance(t, tuple) and len(t) == 2 for t in discovered)
    sources = [s for s, _ in discovered]
    assert "https://stub.local/a/1" not in sources  # provided url excluded
    assert len(sources) == len(set(sources))  # first-seen deduped
    assert all(stance == "supports" for _, stance in discovered)
    assert isinstance(report, Report)


async def test_quick_passes_no_discovered(monkeypatch):
    # Quick must NOT pass discovered (provided-only).
    captured = {}

    async def _spy_check(output, sources, tier, **kwargs):
        captured["discovered"] = kwargs.get("discovered")
        return []

    monkeypatch.setattr(engine_mod, "check_citations", _spy_check)
    await verify({"output": "A. B."}, "quick")
    assert captured["discovered"] is None


async def test_deep_sla_early_stop_yields_partial(monkeypatch):
    # The Deep budget honours the same honest early-stop → partial as Quick.
    fake = FakeLLMProvider(claim_texts=("c1.", "c2.", "c3."), judge_status="unverifiable")
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    # Per-slice clock (Story 3.3): deadline=0; iter1 pre-retrieve=100, post-retrieve=110 (judge
    # c1); iter2 pre-retrieve=5000 (>=1680, break) → one finding.
    monkeypatch.setattr(engine_mod, "_now", _FakeClock([0.0, 100.0, 110.0, 5000.0]))
    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", lambda *a, **k: 1680.0)
    report = await verify({"output": "c1. c2. c3."}, "deep")
    assert len(report.findings) == 1
    assert report.verdict.label == "partial"


# ---------------------------------------------- per-slice timeout granularity (Story 3.3)


async def test_per_slice_retrieve_timeout_stops_early_to_partial(monkeypatch):
    # Story 3.3 AC2: a `retrieve_evidence` slice that never completes trips the per-slice
    # `wait_for` bound (TimeoutError) → the loop stops BEFORE judging → honest `partial`, no
    # raise, no claim judged. The budget is tiny-but-positive; the slice hangs on a never-set
    # Event so the bound ALWAYS fires (deterministic, no real `sleep`, no flake).
    fake = FakeLLMProvider(claim_texts=("c1.", "c2."))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", lambda *a, **k: 0.02)

    never = asyncio.Event()

    async def _hang(*_a, **_k):
        await never.wait()

    monkeypatch.setattr(engine_mod, "retrieve_evidence", _hang)

    report = await verify({"output": "c1. c2."}, "quick")
    assert report.findings == ()  # nothing judged before the bound tripped
    assert fake.judge_calls == 0  # never reached the judge slice
    assert report.verdict.label == "partial"


async def test_per_slice_judge_timeout_stops_early_to_partial(monkeypatch):
    # AC2: retrieve completes (stub, instant) but the JUDGE slice hangs → the second per-slice
    # `wait_for` trips → stop early → `partial`. No finding is appended for the in-flight claim.
    fake = FakeLLMProvider(claim_texts=("c1.",))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", lambda *a, **k: 0.05)

    never = asyncio.Event()

    async def _hang_judge(*_a, **_k):
        await never.wait()

    monkeypatch.setattr(engine_mod, "judge_claim", _hang_judge)

    report = await verify({"output": "c1."}, "quick")
    assert report.findings == ()
    assert report.verdict.label == "partial"


async def test_cancellederror_from_a_slice_propagates(monkeypatch):
    # AC2: `asyncio.CancelledError` (a BaseException, DISTINCT from TimeoutError) raised inside a
    # slice is genuine task cancellation and must PROPAGATE out of `verify` — never swallowed by
    # the per-slice `except asyncio.TimeoutError`.
    fake = FakeLLMProvider(claim_texts=("c1.",))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])

    async def _cancel(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(engine_mod, "retrieve_evidence", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await verify({"output": "c1."}, "quick")


async def test_citation_check_timeout_degrades_to_empty_but_returns(monkeypatch):
    # AC2: the post-loop citation check is bounded by the remaining budget; a hung
    # `check_citations` trips its `wait_for` → citations degrade to () but `verify` STILL returns
    # a Report (the pure/total aggregate always runs). The judged finding survives.
    fake = FakeLLMProvider(claim_texts=("c1.",))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setattr(engine_mod, "_resolve_sla_seconds", lambda *a, **k: 0.05)

    never = asyncio.Event()

    async def _hang_citations(*_a, **_k):
        await never.wait()

    monkeypatch.setattr(engine_mod, "check_citations", _hang_citations)

    report = await verify(
        {"output": "c1.", "sources": [{"url": "https://x.example"}]}, "quick"
    )
    assert report.citations == ()  # degraded after the bound tripped
    assert len(report.findings) == 1  # the judged claim survived
    assert isinstance(report, Report)


# ============================================================ Story 3.4: per-order cost ceiling


async def test_cost_ceiling_stops_early_to_partial(monkeypatch):
    # With a ceiling + a per-claim cost set, the loop accumulates `spent` and stops BEFORE the
    # slice that would breach the ceiling → exactly the claims that fit the budget are judged.
    # ceiling 0.025, claim cost 0.01: judge c1 (spent .01), c2 (spent .02), then .02+.01>.025 → break.
    fake = FakeLLMProvider(claim_texts=("c1.", "c2.", "c3."), judge_status="unverifiable")
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setenv("PROOV_MAX_ORDER_COST_USD", "0.025")
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.01")
    report = await verify({"output": "c1. c2. c3."}, "quick")
    assert len(report.findings) == 2  # only the claims that fit the budget
    assert fake.judge_calls == 2
    assert report.verdict.label == "partial"  # honest early-stop outcome


async def test_cost_ceiling_skips_citation_check_when_budget_spent(monkeypatch):
    # Citation source judging is paid LLM work: once the per-order budget is spent the post-loop
    # citation check is skipped → citations degrade to (). ceiling .01 == claim cost .01 → after
    # judging the one claim, spent .01 >= ceiling .01 → skip.
    fake = FakeLLMProvider(claim_texts=("c1.",), judge_status="supported")
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setenv("PROOV_MAX_ORDER_COST_USD", "0.01")
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.01")

    called = {"n": 0}

    async def _spy_check(*_a, **_k):  # pragma: no cover - must NOT run
        called["n"] += 1
        return [CitationCheck("https://x", True, True, "ok")]

    monkeypatch.setattr(engine_mod, "check_citations", _spy_check)
    report = await verify(
        {"output": "c1.", "sources": [{"url": "https://x.example"}]}, "quick"
    )
    assert called["n"] == 0  # citation check skipped — budget already spent
    assert report.citations == ()
    assert len(report.findings) == 1  # the judged claim survives


async def test_cost_ceiling_default_zero_disables_the_meter(monkeypatch):
    # The default ceiling 0.0 DISABLES the meter: even an absurd per-claim cost is inert, so the
    # $0 free-tier path is byte-for-byte unchanged — ALL claims are judged, no early stop.
    fake = FakeLLMProvider(claim_texts=("c1.", "c2.", "c3."))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.delenv("PROOV_MAX_ORDER_COST_USD", raising=False)
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "999.0")  # would breach any real ceiling
    report = await verify({"output": "c1. c2. c3."}, "quick")
    assert len(report.findings) == 3  # meter inert → every claim judged
    assert fake.judge_calls == 3


async def test_engine_threads_provider_chain_and_stamps_head_model(monkeypatch):
    # The engine resolves the chain ONCE and passes `providers=chain` into extraction (and
    # judgment), stamping `model` from the chain HEAD (the advertised primary, OQ1).
    fake = FakeLLMProvider(claim_texts=("c1.",))
    chain = [fake]
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: chain)

    real_extract = engine_mod.extract_claims
    seen = {}

    async def _spy_extract(text, tier, *, provider=None, providers=None, options=None, explicit_claims=None):
        seen["providers"] = providers
        return await real_extract(
            text, tier, providers=providers, options=options, explicit_claims=explicit_claims
        )

    monkeypatch.setattr(engine_mod, "extract_claims", _spy_extract)
    report = await verify({"output": "c1."}, "quick")
    assert seen["providers"] is chain  # the resolved chain is threaded into extraction
    assert report.model == "fake-model"  # head-of-chain model stamped


def test_resolve_max_order_cost_hardening():
    # Default 0.0 (disabled); garbage / non-finite / negative → 0.0; a valid >=0 value honoured.
    assert engine_mod._resolve_max_order_cost(None) == 0.0
    assert engine_mod._resolve_max_order_cost("0.05") == 0.05
    assert engine_mod._resolve_max_order_cost("0") == 0.0
    assert engine_mod._resolve_max_order_cost("nonsense") == 0.0
    assert engine_mod._resolve_max_order_cost("inf") == 0.0
    assert engine_mod._resolve_max_order_cost("nan") == 0.0
    assert engine_mod._resolve_max_order_cost("-1") == 0.0


# ============================================== Story 3.4 code-review patches (P1/P2/P4 regressions)


async def test_deep_cost_meter_accounts_for_multi_pass_fan_out(monkeypatch):
    # P2: a Deep claim is judged by N self-consistency passes, so its METERED marginal cost must be
    # (per-pass cost × passes), not one pass — otherwise the ceiling silently under-bounds Deep spend
    # by ~the pass count. passes=2, per-pass cost .01 → effective claim cost .02. ceiling .03: claim1
    # → spent .02 (fits); claim2 would push .04 > .03 → break. Exactly ONE claim fits the multi-pass
    # aware budget (without the fix, a flat .01 claim cost would have let all 3 through).
    fake = FakeLLMProvider(claim_texts=("c1.", "c2.", "c3."), judge_status="unverifiable")
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setenv("PROOV_DEEP_JUDGE_PASSES", "2")
    monkeypatch.setenv("PROOV_DEEP_CLAIM_COST_USD", "0.01")
    monkeypatch.setenv("PROOV_MAX_ORDER_COST_USD", "0.03")
    report = await verify({"output": "c1. c2. c3."}, "deep")
    assert len(report.findings) == 1  # only one claim fit the multi-pass-aware budget
    assert fake.judge_calls == 2  # 1 claim judged × 2 passes (claim2 breaks before any judge call)
    assert report.verdict.label == "partial"  # honest early-stop (unverifiable → partial)


async def test_cost_ceiling_below_one_claim_warns_and_judges_nothing(monkeypatch, caplog):
    # P4: a ceiling smaller than a single claim's marginal cost can never judge a claim — the loop
    # breaks at index 0 → empty partial. The engine emits a one-time warning so this misconfiguration
    # is visible rather than a silent empty result indistinguishable from a real degrade.
    fake = FakeLLMProvider(claim_texts=("c1.", "c2."))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setenv("PROOV_MAX_ORDER_COST_USD", "0.001")
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.01")  # one claim already exceeds the ceiling
    with caplog.at_level("WARNING"):
        report = await verify({"output": "c1. c2."}, "quick")
    assert report.findings == ()  # nothing fit the budget
    assert fake.judge_calls == 0
    assert report.verdict.label == "partial"
    assert any("exceeds the per-order ceiling" in r.message for r in caplog.records)


async def test_cost_ceiling_caps_citation_sources_to_remaining_budget(monkeypatch):
    # P1: citation source judging is paid LLM work, so the engine bounds the citation check by the
    # REMAINING budget — it passes max_paid_sources=int((ceiling-spent)/claim_cost) into
    # check_citations. ceiling .055, claim cost .01, one claim judged (spent .01) → remaining .045 →
    # 4.5 → int 4 affordable provided sources (the midpoint value is robust to float rounding).
    fake = FakeLLMProvider(claim_texts=("c1.",), judge_status="supported")
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.setenv("PROOV_MAX_ORDER_COST_USD", "0.055")
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.01")

    seen = {}

    async def _spy_check(output, sources, tier, **kwargs):
        seen["max_paid_sources"] = kwargs.get("max_paid_sources")
        return []

    monkeypatch.setattr(engine_mod, "check_citations", _spy_check)
    await verify({"output": "c1.", "sources": [{"url": "https://x.example"}]}, "quick")
    assert seen["max_paid_sources"] == 4  # (0.055 - 0.01) / 0.01 = 4.5 → 4


async def test_citation_max_paid_sources_is_none_when_meter_disabled(monkeypatch):
    # P1: with the default ceiling 0.0 the meter is OFF, so the engine imposes NO budget cap on the
    # citation check — max_paid_sources is None (unbounded). The $0 free-tier path is unchanged.
    fake = FakeLLMProvider(claim_texts=("c1.",))
    monkeypatch.setattr(engine_mod, "default_llm_chain", lambda *a, **k: [fake])
    monkeypatch.delenv("PROOV_MAX_ORDER_COST_USD", raising=False)
    monkeypatch.setenv("PROOV_QUICK_CLAIM_COST_USD", "0.01")

    seen = {}

    async def _spy_check(output, sources, tier, **kwargs):
        seen["max_paid_sources"] = kwargs.get("max_paid_sources")
        return []

    monkeypatch.setattr(engine_mod, "check_citations", _spy_check)
    await verify({"output": "c1.", "sources": [{"url": "https://x.example"}]}, "quick")
    assert seen["max_paid_sources"] is None

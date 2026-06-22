"""Tests for the pure engine value types (`proov/types.py`).

Straight unit tests — no I/O, mirroring `tests/test_validation.py` / `test_receipt.py`.
"""

from __future__ import annotations

import dataclasses

import pytest

from proov.types import (
    DEEP_EVIDENCE_K,
    DEEP_MAX_CLAIMS,
    QUICK_EVIDENCE_K,
    QUICK_MAX_CLAIMS,
    Claim,
    Evidence,
    EvidenceStance,
    Judgment,
    clamp_confidence,
    evidence_k_for_tier,
    max_claims_for_tier,
)


def test_tier_ceilings():
    assert QUICK_MAX_CLAIMS == 20
    assert DEEP_MAX_CLAIMS == 50
    assert max_claims_for_tier("quick") == 20
    assert max_claims_for_tier("deep") == 50


def test_options_max_claims_lowers_cap():
    assert max_claims_for_tier("deep", {"max_claims": 5}) == 5
    assert max_claims_for_tier("quick", {"max_claims": 3}) == 3


def test_options_max_claims_cannot_raise_above_tier_ceiling():
    assert max_claims_for_tier("quick", {"max_claims": 999}) == 20
    assert max_claims_for_tier("deep", {"max_claims": 999}) == 50


@pytest.mark.parametrize("bad", [0, -1, "x", None, True, 2.5])
def test_options_max_claims_invalid_is_ignored(bad):
    assert max_claims_for_tier("quick", {"max_claims": bad}) == 20


def test_options_missing_max_claims_is_ignored():
    assert max_claims_for_tier("deep", {}) == 50
    assert max_claims_for_tier("deep", None) == 50


def test_unknown_tier_defaults_to_quick_ceiling():
    # `tier_for_service` is permissive (unknown → "quick"); the cap mirrors that.
    assert max_claims_for_tier("nonsense") == 20  # type: ignore[arg-type]


def test_claim_is_frozen():
    claim = Claim(id="c1", text="Paris is the capital of France")
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.text = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------- Evidence


def test_evidence_is_frozen():
    ev = Evidence(source="https://x", title="X", snippet="some text")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.snippet = "mutated"  # type: ignore[misc]


def test_evidence_score_defaults_to_none():
    ev = Evidence(source="https://x", title="X", snippet="text")
    assert ev.score is None


def test_evidence_has_no_stance_field():
    # Stance is a *judgment* output (Story 2.3), never a retrieval fact — guard against it
    # creeping back into the raw retrieved type.
    fields = {f.name for f in dataclasses.fields(Evidence)}
    assert fields == {"source", "title", "snippet", "score"}


# --------------------------------------------------------------------------- evidence_k_for_tier


def test_evidence_k_ceilings():
    assert QUICK_EVIDENCE_K == 3
    assert DEEP_EVIDENCE_K == 6
    assert evidence_k_for_tier("quick") == 3
    assert evidence_k_for_tier("deep") == 6


def test_evidence_k_options_lower_cap():
    assert evidence_k_for_tier("deep", {"max_evidence": 2}) == 2
    assert evidence_k_for_tier("quick", {"k": 1}) == 1


def test_evidence_k_options_cannot_raise_above_tier_ceiling():
    assert evidence_k_for_tier("quick", {"max_evidence": 999}) == 3
    assert evidence_k_for_tier("deep", {"k": 999}) == 6


@pytest.mark.parametrize("bad", [0, -1, "x", None, True, 2.5])
def test_evidence_k_invalid_option_is_ignored(bad):
    assert evidence_k_for_tier("deep", {"max_evidence": bad}) == 6


def test_evidence_k_missing_option_is_ignored():
    assert evidence_k_for_tier("deep", {}) == 6
    assert evidence_k_for_tier("quick", None) == 3


def test_evidence_k_unknown_tier_defaults_to_quick():
    assert evidence_k_for_tier("nonsense") == 3  # type: ignore[arg-type]


# --------------------------------------------------------------------------- EvidenceStance / Judgment


def test_evidence_stance_is_frozen():
    es = EvidenceStance(source="https://x", quote="q", stance="supports")
    with pytest.raises(dataclasses.FrozenInstanceError):
        es.quote = "mutated"  # type: ignore[misc]


def test_judgment_is_frozen_and_hashable():
    j = Judgment(status="supported", confidence=0.9)
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.status = "unsupported"  # type: ignore[misc]
    # tuple evidence keeps Judgment hashable like Claim/Evidence.
    assert hash(j) == hash(Judgment(status="supported", confidence=0.9))


def test_judgment_evidence_defaults_to_empty_tuple():
    j = Judgment(status="unverifiable", confidence=0.0)
    assert j.evidence == ()
    assert isinstance(j.evidence, tuple)


# --------------------------------------------------------------------------- clamp_confidence


def test_clamp_confidence_passes_through_mid_range():
    assert clamp_confidence(0.5) == 0.5


def test_clamp_confidence_clamps_both_ends():
    assert clamp_confidence(1.5) == 1.0
    assert clamp_confidence(-1) == 0.0


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), "x", None, True, False])
def test_clamp_confidence_rejects_non_finite_bool_and_non_numeric(bad):
    assert clamp_confidence(bad) == 0.0


def test_clamp_confidence_always_returns_float():
    # 0 vs 0.0 canonicalise to different bytes when hashed (Story 2.6) — must be float.
    result = clamp_confidence(0)
    assert type(result) is float
    assert result == 0.0
    assert type(clamp_confidence(1)) is float

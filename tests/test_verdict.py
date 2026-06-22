"""Tests for the pure deterministic verdict aggregator (`proov.verdict`).

Offline pure unit tests — no socket, no Gemini, no `httpx`/`MockTransport`, no async marker.
Mirrors the `tests/test_types.py` / `tests/test_receipt.py` harness, NOT the `MockTransport`
harness of `test_search.py`/`test_citations.py`. Covers the `Verdict` type, the full FR10
truth table, the overall-confidence mean, the PRD §6 `stats` counts, defensive/total
behaviour on unknown + empty inputs, and the load-bearing determinism guarantee.
"""

from __future__ import annotations

import dataclasses
import math
import typing

import pytest

from proov.types import CitationCheck, Judgment, Verdict, VerdictLabel
from proov.verdict import aggregate_verdict


# --- small builders ---------------------------------------------------------------------


def _j(status: str, confidence: float = 1.0) -> Judgment:
    """A `Judgment` with no evidence (the aggregator reads `status`/`confidence` only)."""
    return Judgment(status=status, confidence=confidence)  # type: ignore[arg-type]


def _c(flag: str, *, source: str = "https://example.com", retrievable: bool = True,
       supports: bool = True) -> CitationCheck:
    return CitationCheck(source=source, retrievable=retrievable,
                         supports_attached_claim=supports, flag=flag)  # type: ignore[arg-type]


# --- the Verdict type (AC1) -------------------------------------------------------------


def test_verdict_is_frozen():
    v = aggregate_verdict([_j("supported")], [])
    assert isinstance(v, Verdict)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.label = "fail"  # type: ignore[misc]


def test_verdict_field_order_matches_prd_stats_shape():
    fields = [f.name for f in dataclasses.fields(Verdict)]
    assert fields == [
        "label", "confidence", "claims_total", "supported", "unsupported", "unverifiable"
    ]
    # The four count fields are EXACTLY the PRD §6 `stats` object.
    assert fields[2:] == ["claims_total", "supported", "unsupported", "unverifiable"]


def test_verdict_label_literal_membership():
    args = set(typing.get_args(VerdictLabel))
    assert args == {"pass", "fail", "partial"}
    # `"unverifiable"` is the degrade verdict, NOT an aggregation label — not in the type.
    assert "unverifiable" not in args


# --- fail rows (AC3 / AC8) --------------------------------------------------------------


def test_fail_on_fabricated_citation_with_all_supported_claims():
    v = aggregate_verdict([_j("supported"), _j("supported")], [_c("fabricated")])
    assert v.label == "fail"


def test_fail_on_unsupported_claim_with_no_citations():
    v = aggregate_verdict([_j("supported"), _j("unsupported")], [])
    assert v.label == "fail"


def test_fail_on_both_fabricated_and_unsupported():
    v = aggregate_verdict([_j("unsupported")], [_c("fabricated")])
    assert v.label == "fail"


def test_fail_on_fabricated_citation_with_zero_claims():
    # Fabricated fails even with nothing else to judge.
    v = aggregate_verdict([], [_c("fabricated")])
    assert v.label == "fail"
    assert v.claims_total == 0


# --- pass row (AC3 / AC8) ---------------------------------------------------------------


def test_pass_on_all_supported_no_fabricated():
    v = aggregate_verdict([_j("supported"), _j("supported")], [_c("ok")])
    assert v.label == "pass"


def test_pass_on_all_supported_empty_citations():
    v = aggregate_verdict([_j("supported")], [])
    assert v.label == "pass"


# --- partial rows (AC3 / AC8) -----------------------------------------------------------


def test_partial_on_zero_claims_is_not_pass():
    v = aggregate_verdict([], [])
    assert v.label == "partial"
    assert v.label != "pass"


def test_partial_on_zero_claims_with_ok_citation():
    v = aggregate_verdict([], [_c("ok")])
    assert v.label == "partial"


def test_partial_on_supported_plus_unverifiable_mix():
    v = aggregate_verdict([_j("supported"), _j("unverifiable")], [])
    assert v.label == "partial"


def test_partial_on_all_unverifiable():
    v = aggregate_verdict([_j("unverifiable"), _j("unverifiable")], [])
    assert v.label == "partial"


# --- misattributed default (OQ1, AC8) ---------------------------------------------------


def test_misattributed_citation_does_not_gate_verdict():
    # v1 literal-FR10: a misattributed citation alongside all-supported claims is still `pass`.
    v = aggregate_verdict([_j("supported")], [_c("misattributed")])
    assert v.label == "pass"


# --- confidence (AC5 / AC8) -------------------------------------------------------------


def test_confidence_is_mean_of_per_claim_confidences():
    v = aggregate_verdict([_j("supported", 1.0), _j("unverifiable", 0.0)], [])
    assert v.confidence == 0.5


def test_confidence_zero_claims_is_float_zero():
    v = aggregate_verdict([], [])
    assert v.confidence == 0.0
    assert isinstance(v.confidence, float)
    # The canonicalisation byte-trap guard: `0.0` not `0`.
    assert repr(v.confidence) == "0.0"


def test_confidence_always_float_on_populated_verdict():
    v = aggregate_verdict([_j("supported", 1.0)], [])
    assert isinstance(v.confidence, float)


def test_confidence_clamps_out_of_range_and_nan():
    # A Judgment carrying out-of-range / nan confidence still yields a finite [0,1] float.
    v = aggregate_verdict([_j("supported", 2.0), _j("supported", float("nan"))], [])
    assert isinstance(v.confidence, float)
    assert 0.0 <= v.confidence <= 1.0
    assert math.isfinite(v.confidence)
    # 2.0 → clamped 1.0, nan → 0.0, mean = 0.5.
    assert v.confidence == 0.5


def test_confidence_independent_of_label():
    # A `fail` can carry high confidence — it is confidence in the judgments, not in passing.
    v = aggregate_verdict([_j("unsupported", 1.0)], [])
    assert v.label == "fail"
    assert v.confidence == 1.0


# --- counts == PRD §6 stats (AC4 / AC8) -------------------------------------------------


def test_counts_match_input_mix():
    judgments = [
        _j("supported"), _j("supported"), _j("unsupported"),
        _j("unverifiable"), _j("unverifiable"), _j("unverifiable"),
    ]
    v = aggregate_verdict(judgments, [])
    assert v.claims_total == 6
    assert v.supported == 2
    assert v.unsupported == 1
    assert v.unverifiable == 3


# --- defensive / total (AC3 / AC4 / AC8) ------------------------------------------------


def test_unknown_status_counts_as_unverifiable_and_not_fail():
    v = aggregate_verdict([_j("bogus"), _j("supported")], [])
    assert v.unverifiable == 1
    assert v.unsupported == 0
    # Unknown status must NOT trigger fail; with a real supported + an unverifiable → partial.
    assert v.label == "partial"


def test_unknown_citation_flag_is_non_fabricated():
    v = aggregate_verdict([_j("supported")], [_c("weird-flag")])
    assert v.label == "pass"


def test_empty_inputs_never_raise_and_return_partial():
    v = aggregate_verdict([], [])
    assert isinstance(v, Verdict)
    assert v.label == "partial"
    assert (v.claims_total, v.supported, v.unsupported, v.unverifiable) == (0, 0, 0, 0)


# --- determinism (AC6 / AC8) ------------------------------------------------------------


def test_repeated_calls_are_identical():
    judgments = [_j("supported", 0.9), _j("unverifiable", 0.4)]
    citations = [_c("ok"), _c("misattributed")]
    first = aggregate_verdict(judgments, citations)
    second = aggregate_verdict(judgments, citations)
    assert first == second


def test_shuffled_judgments_yield_identical_verdict():
    # All-equal confidences so the `sum` order is irrelevant for the float.
    a = [_j("supported", 0.5), _j("unverifiable", 0.5), _j("supported", 0.5)]
    b = [a[2], a[0], a[1]]
    va = aggregate_verdict(a, [])
    vb = aggregate_verdict(b, [])
    assert va == vb
    assert va.label == "partial"
    assert va.confidence == 0.5

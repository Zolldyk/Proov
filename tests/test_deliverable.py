"""Tests for the stub deliverable builder and the service→tier mapping.

These cover the pure, SDK-agnostic helpers (`proov.deliverable`, `proov.services`) — no
socket, no platform, no SDK internals.
"""

from __future__ import annotations

import json

from proov import services
from proov.deliverable import (
    build_deliverable,
    build_graceful_deliverable,
    build_stub_deliverable,
)
from proov.receipt import canonical_json, keccak256_hex
from proov.services import (
    DEEP_SERVICE_ID,
    QUICK_SERVICE_ID,
    tier_for_service,
)
from proov.types import (
    CitationCheck,
    Claim,
    ClaimFinding,
    EvidenceStance,
    Judgment,
    Report,
    Verdict,
)

# PRD §6 receipt contract — the eight keys the populated receipt must carry.
_RECEIPT_KEYS = {
    "output_hash",
    "report_hash",
    "verdict",
    "confidence",
    "model",
    "version",
    "timestamp",
    "anchor_ref",
}

# PRD §6 deliverable contract — every top-level key must be present (schema-valid shape).
_PRD6_KEYS = {
    "verdict",
    "confidence",
    "summary",
    "claims",
    "citations_checked",
    "stats",
    "receipt",
    "disclaimer",
}


def test_build_stub_deliverable_has_all_prd6_keys():
    payload = build_stub_deliverable(order=object(), tier="quick")
    assert _PRD6_KEYS.issubset(payload.keys())


def test_build_stub_deliverable_is_json_serialisable():
    payload = build_stub_deliverable(order=object(), tier="deep")
    # Must round-trip through json.dumps (the provider serialises it for deliver_order).
    roundtripped = json.loads(json.dumps(payload))
    assert roundtripped == payload


def test_build_stub_deliverable_marks_unverifiable_stub():
    payload = build_stub_deliverable(order=object(), tier="quick")
    # Engine is pending — must NOT assert a pass/fail verdict from a stub.
    assert payload["verdict"] == "unverifiable"
    assert payload["confidence"] == 0.0
    assert payload["claims"] == []
    assert payload["citations_checked"] == []
    assert isinstance(payload["disclaimer"], str) and payload["disclaimer"]


def test_receipt_is_populated_and_carries_eight_keys():
    payload = build_stub_deliverable(order=object(), tier="quick", output_text="x")
    receipt = payload["receipt"]
    # No longer the Story 1.3 `{}` placeholder — a real, populated receipt.
    assert receipt != {}
    assert set(receipt.keys()) == _RECEIPT_KEYS
    # Producing engine identity is the honest stub id + the single-sourced version.
    assert receipt["model"] == "stub-no-engine"
    assert receipt["version"] == "0.1.0"


def test_receipt_verdict_confidence_mirror_top_level():
    payload = build_stub_deliverable(order=object(), tier="deep", output_text="x")
    assert payload["receipt"]["verdict"] == payload["verdict"]
    assert payload["receipt"]["confidence"] == payload["confidence"]


def test_receipt_output_hash_is_keccak_of_output_text():
    output_text = "The Eiffel Tower is in Berlin."
    payload = build_stub_deliverable(order=object(), tier="quick", output_text=output_text)
    assert payload["receipt"]["output_hash"] == keccak256_hex(output_text.encode("utf-8"))


# Pre-1.6 known-answer report_hash for (tier="quick", output_text="x"). The badge is a
# sibling added AFTER the receipt over the UNCHANGED report body, so this value must not
# move (regression: the badge must not perturb the receipt the 1.4 anchor hashes).
_STUB_QUICK_X_REPORT_HASH = (
    "0x965dd1a08d50d1732f07dc907fb5af5cab903ff9e4219315780751e80989bc58"
)


def test_receipt_report_hash_matches_keccak_of_body_without_receipt():
    payload = build_stub_deliverable(order=object(), tier="quick", output_text="x")
    # Story 1.6: the report body strips BOTH sibling artifact keys (`receipt` and the new
    # `verified_by_proov`) before re-canonicalising.
    body = {k: v for k, v in payload.items() if k not in ("receipt", "verified_by_proov")}
    expected = keccak256_hex(canonical_json(body).encode("utf-8"))
    assert payload["receipt"]["report_hash"] == expected
    # Regression: the badge sibling did NOT change the known-answer report_hash value.
    assert payload["receipt"]["report_hash"] == _STUB_QUICK_X_REPORT_HASH


def test_stub_deliverable_carries_in_band_verified_by_proov_badge():
    payload = build_stub_deliverable(order=object(), tier="quick", output_text="x")
    badge = payload["verified_by_proov"]
    receipt = payload["receipt"]
    # In-band badge mirrors the embedded receipt, with no concrete anchor yet.
    assert badge["anchor"] is None
    assert badge["receipt_id"] == receipt["report_hash"]
    assert badge["output_hash"] == receipt["output_hash"]
    assert badge["report_hash"] == receipt["report_hash"]
    assert badge["verdict"] == receipt["verdict"]
    assert badge["schema"] == "proov.verified-by-proov.v1"


def test_graceful_deliverable_has_all_prd6_keys_and_real_receipt():
    payload = build_graceful_deliverable(
        order=object(), tier="quick", output_text="x", reason="internal_verification_error"
    )
    # Same PRD §6 shape + populated real receipt as the happy stub (degrade, don't drop).
    assert _PRD6_KEYS.issubset(payload.keys())
    receipt = payload["receipt"]
    assert receipt != {}
    assert set(receipt.keys()) == _RECEIPT_KEYS


def test_graceful_deliverable_is_unverifiable_with_float_zero_confidence():
    payload = build_graceful_deliverable(
        order=object(), tier="deep", reason="internal_verification_error"
    )
    assert payload["verdict"] == "unverifiable"
    # Must be a float 0.0 — `0` vs `0.0` canonicalise to different bytes → different report_hash.
    assert payload["confidence"] == 0.0
    assert isinstance(payload["confidence"], float)
    # Degrade is flagged in stats so an inspector sees it was not a real verdict.
    assert payload["stats"].get("degraded") is True
    assert payload["stats"]["tier"] == "deep"


def test_graceful_deliverable_allows_partial_verdict():
    payload = build_graceful_deliverable(
        order=object(), tier="quick", reason="partial_progress", verdict="partial"
    )
    assert payload["verdict"] == "partial"
    assert payload["receipt"]["verdict"] == "partial"


def test_graceful_deliverable_summary_is_honest_and_carries_reason_no_stack_trace():
    reason = "internal_verification_error"
    payload = build_graceful_deliverable(order=object(), tier="quick", reason=reason)
    summary = payload["summary"]
    assert reason in summary
    # Honest, not a leak: no traceback / no exception class noise.
    assert "Traceback" not in summary
    assert "could not" in summary.lower() or "unable" in summary.lower()


def test_graceful_deliverable_report_hash_reproducible_from_body():
    payload = build_graceful_deliverable(
        order=object(), tier="quick", output_text="x", reason="internal_verification_error"
    )
    # Strip BOTH siblings (Story 1.6) before re-canonicalising.
    body = {k: v for k, v in payload.items() if k not in ("receipt", "verified_by_proov")}
    expected = keccak256_hex(canonical_json(body).encode("utf-8"))
    assert payload["receipt"]["report_hash"] == expected


def test_graceful_deliverable_carries_in_band_verified_by_proov_badge():
    payload = build_graceful_deliverable(
        order=object(), tier="deep", output_text="x", reason="internal_verification_error"
    )
    badge = payload["verified_by_proov"]
    assert badge["anchor"] is None
    assert badge["receipt_id"] == payload["receipt"]["report_hash"]
    assert badge["output_hash"] == payload["receipt"]["output_hash"]
    assert badge["verdict"] == payload["receipt"]["verdict"]


def test_graceful_deliverable_output_hash_is_keccak_of_output_text():
    output_text = "The Eiffel Tower is in Berlin."
    payload = build_graceful_deliverable(
        order=object(), tier="quick", output_text=output_text, reason="x"
    )
    assert payload["receipt"]["output_hash"] == keccak256_hex(output_text.encode("utf-8"))


def _sample_report(*, confidence=0.9, model="gemini-2.5-flash") -> Report:
    """A small real `Report`: one supported claim with grounded evidence + one ok citation."""
    finding = ClaimFinding(
        claim=Claim(id="c1", text="Paris is the capital of France."),
        judgment=Judgment(
            status="supported",
            confidence=confidence,
            evidence=(
                EvidenceStance(
                    source="https://stub.local/paris/1",
                    quote="Paris is the capital of France",
                    stance="supports",
                ),
            ),
        ),
    )
    citation = CitationCheck(
        source="https://stub.local/paris/1",
        retrievable=True,
        supports_attached_claim=True,
        flag="ok",
    )
    verdict = Verdict(
        label="pass",
        confidence=confidence,
        claims_total=1,
        supported=1,
        unsupported=0,
        unverifiable=0,
    )
    return Report(verdict=verdict, findings=(finding,), citations=(citation,), model=model)


def test_build_deliverable_has_all_prd6_keys():
    payload = build_deliverable(object(), "quick", output_text="x", report=_sample_report())
    assert _PRD6_KEYS.issubset(payload.keys())
    assert "verified_by_proov" in payload


def test_build_deliverable_maps_the_real_verdict_and_confidence():
    report = _sample_report()
    payload = build_deliverable(object(), "quick", output_text="x", report=report)
    # The REAL aggregated verdict, not the stub `unverifiable`.
    assert payload["verdict"] == "pass"
    assert payload["confidence"] == report.verdict.confidence


def test_build_deliverable_stats_is_tier_plus_four_counts():
    payload = build_deliverable(object(), "deep", output_text="x", report=_sample_report())
    assert payload["stats"] == {
        "tier": "deep",
        "claims_total": 1,
        "supported": 1,
        "unsupported": 0,
        "unverifiable": 0,
    }


def test_build_deliverable_per_claim_finding_shape():
    payload = build_deliverable(object(), "quick", output_text="x", report=_sample_report())
    assert len(payload["claims"]) == 1
    claim = payload["claims"][0]
    assert set(claim.keys()) == {"id", "claim", "status", "confidence", "evidence"}
    assert claim["id"] == "c1"
    assert claim["claim"] == "Paris is the capital of France."
    assert claim["status"] == "supported"
    ev = claim["evidence"][0]
    assert set(ev.keys()) == {"source", "quote", "stance"}
    assert ev["stance"] == "supports"


def test_build_deliverable_confidences_stay_float_even_when_zero():
    # Every confidence (top-level + per-claim) must stay a float — `0` vs `0.0` canonicalise
    # to different bytes, which would shift report_hash.
    payload = build_deliverable(
        object(), "quick", output_text="x", report=_sample_report(confidence=0.0)
    )
    assert isinstance(payload["confidence"], float)
    assert payload["confidence"] == 0.0
    assert isinstance(payload["claims"][0]["confidence"], float)
    assert payload["claims"][0]["confidence"] == 0.0


def test_build_deliverable_citations_checked_shape():
    payload = build_deliverable(object(), "quick", output_text="x", report=_sample_report())
    assert len(payload["citations_checked"]) == 1
    c = payload["citations_checked"][0]
    assert set(c.keys()) == {"source", "retrievable", "supports_attached_claim", "flag"}
    assert c["flag"] == "ok"


def test_build_deliverable_receipt_carries_real_model_and_is_reproducible():
    report = _sample_report(model="gemini-2.5-flash")
    payload = build_deliverable(object(), "quick", output_text="hello", report=report)
    receipt = payload["receipt"]
    # FR14: the REAL model id, not the stub `stub-no-engine`.
    assert receipt["model"] == "gemini-2.5-flash"
    assert receipt["version"] == "0.1.0"
    assert receipt["output_hash"] == keccak256_hex("hello".encode("utf-8"))
    # report_hash reproduces over the body minus BOTH artifact siblings.
    body = {k: v for k, v in payload.items() if k not in ("receipt", "verified_by_proov")}
    assert receipt["report_hash"] == keccak256_hex(canonical_json(body).encode("utf-8"))


def test_build_deliverable_is_json_serialisable():
    payload = build_deliverable(object(), "quick", output_text="x", report=_sample_report())
    assert json.loads(json.dumps(payload)) == payload


def test_build_deliverable_carries_in_band_badge_with_real_verdict():
    payload = build_deliverable(object(), "quick", output_text="x", report=_sample_report())
    badge = payload["verified_by_proov"]
    assert badge["anchor"] is None
    assert badge["receipt_id"] == payload["receipt"]["report_hash"]
    assert badge["verdict"] == "pass"
    assert badge["schema"] == "proov.verified-by-proov.v1"


def test_tier_for_service_maps_known_ids():
    assert tier_for_service(QUICK_SERVICE_ID) == "quick"
    assert tier_for_service(DEEP_SERVICE_ID) == "deep"


def test_tier_for_service_defaults_quick_for_unknown():
    assert tier_for_service("svc-does-not-exist") == "quick"


def test_tier_for_service_honours_env_override(monkeypatch):
    monkeypatch.setenv("PROOV_DEEP_SERVICE_ID", "svc-custom-deep")
    # Re-read happens per call, so the override takes effect immediately.
    assert services.tier_for_service("svc-custom-deep") == "deep"

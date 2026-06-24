"""Tests for the companion "Research" caller composition core (`proov/companion.py`, Story 4.2).

Fully offline ($0, NFR3): the suite-wide `conftest.py` disables the cache/ledger, and these
tests build a real `Report` → `build_deliverable` directly (the same construction
`tests/test_deliverable.py` uses) — NO socket is ever bound and NO live order is placed. The
live `scripts/research_caller.py` buyer runner is a thin shell (not unit-tested directly), so
only the pure functions in `companion.py` are exercised here (the established `proov/` core vs
`scripts/` runner split).
"""

from __future__ import annotations

import inspect
import json

from proov import companion
from proov.badge import BADGE_SCHEMA
from proov.companion import (
    build_proov_input,
    compose_delivery,
    extract_verified_artifact,
    make_research_output,
)
from proov.deliverable import build_deliverable
from proov.types import (
    CitationCheck,
    Claim,
    ClaimFinding,
    EvidenceStance,
    Judgment,
    Report,
    Verdict,
)


def _sample_report(*, confidence=0.9, model="gemini-2.5-flash") -> Report:
    """A small real `Report`: one supported claim with grounded evidence + one ok citation.

    Mirrors `tests/test_deliverable.py::_sample_report` so `extract_verified_artifact` is tested
    against a REAL `build_deliverable(...)` output, not a hand-built deliverable.
    """
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


def _real_deliverable(*, output_text="Paris is the capital of France.") -> dict:
    return build_deliverable(None, "quick", output_text=output_text, report=_sample_report())


# --- AC1: the composition core is SDK-agnostic (no croo) --------------------------------------


def test_companion_module_does_not_import_croo():
    # Architecture §2: only provider.py is CROO-coupled. The composition core must stay SDK-agnostic
    # (the buyer wiring lives in scripts/research_caller.py).
    source = inspect.getsource(companion)
    assert "import croo" not in source
    assert "from croo" not in source


# --- Task 1 / AC2-AC3: build_proov_input → the PRD §6 requirements shape -----------------------


def test_build_proov_input_basic_shape():
    payload = build_proov_input("The sky is blue.")
    assert payload == {"output": "The sky is blue.", "mode": "quick"}


def test_build_proov_input_attaches_sources_as_url_objects():
    payload = build_proov_input("x", ["https://a.example", "  https://b.example  "])
    assert payload["sources"] == [
        {"url": "https://a.example"},
        {"url": "https://b.example"},
    ]
    # JSON-encodable for NegotiateOrderRequest.requirements.
    assert json.loads(json.dumps(payload)) == payload


def test_build_proov_input_omits_empty_sources():
    # Blank/None sources → no `sources` key (the validator's optional-sources rule never trips).
    assert "sources" not in build_proov_input("x")
    assert "sources" not in build_proov_input("x", [])
    assert "sources" not in build_proov_input("x", ["   ", ""])


# --- Task 1 / AC6: make_research_output is thin, keyless, deterministic ------------------------


def test_make_research_output_keyless_returns_nonempty_sample():
    out = make_research_output()
    assert isinstance(out, str) and out.strip()


def test_make_research_output_returns_caller_supplied_text():
    assert make_research_output("Custom claim to verify.") == "Custom claim to verify."
    # Blank/whitespace topic falls back to the built-in sample.
    assert make_research_output("   ") == make_research_output()


# --- Task 1 / AC4: extract_verified_artifact against a REAL build_deliverable output -----------


def test_extract_tx_bearing_artifact_when_content_hash_present():
    deliverable = _real_deliverable()
    artifact = extract_verified_artifact(
        deliverable,
        content_hash="0xcontenthash",
        deliver_tx_hash="0xdeadbeef",
        order_id="ord-1",
        delivery_id="dlv-1",
    )
    # Post-delivery, tx-bearing form: the on-chain content_hash is the canonical receipt id.
    assert artifact["schema"] == BADGE_SCHEMA
    assert artifact["receipt_id"] == "0xcontenthash"
    assert artifact["anchor"]["content_hash"] == "0xcontenthash"
    assert artifact["anchor"]["deliver_tx_hash"] == "0xdeadbeef"
    assert artifact["anchor"]["explorer_url"] == "https://basescan.org/tx/0xdeadbeef"
    # The verdict identity flows through from the real receipt.
    assert artifact["verdict"] == "pass"


def test_extract_in_band_badge_when_no_anchor():
    deliverable = _real_deliverable()
    artifact = extract_verified_artifact(deliverable)  # no content_hash → in-band
    assert artifact["anchor"] is None
    # In-band receipt_id is the pre-delivery-stable report_hash carried in the receipt.
    assert artifact["receipt_id"] == deliverable["receipt"]["report_hash"]
    assert artifact["schema"] == BADGE_SCHEMA


def test_extract_returned_artifact_is_a_copy_of_the_in_band_badge():
    # Mutating the extracted artifact must not mutate the deliverable's carried badge.
    deliverable = _real_deliverable()
    artifact = extract_verified_artifact(deliverable)
    artifact["receipt_id"] = "tampered"
    assert deliverable["verified_by_proov"]["receipt_id"] != "tampered"


# --- Task 1 / AC8: degrade path — missing receipt/badge → structured signal, never raises ------


def test_extract_missing_receipt_and_badge_returns_structured_error():
    artifact = extract_verified_artifact({"verdict": "pass"})  # no receipt, no badge
    assert isinstance(artifact, dict)
    assert artifact.get("error") == "no_receipt"
    # It is NOT a real badge (no schema) → compose treats it as unverified.
    assert artifact.get("schema") != BADGE_SCHEMA


def test_extract_non_dict_deliverable_returns_structured_error():
    assert extract_verified_artifact(None)["error"] == "no_deliverable"  # type: ignore[arg-type]


def test_extract_falls_back_to_in_band_badge_when_receipt_missing():
    # A deliverable that lost its `receipt` but kept the in-band badge → return the badge.
    deliverable = _real_deliverable()
    badge = deliverable["verified_by_proov"]
    artifact = extract_verified_artifact({"verified_by_proov": badge})
    assert artifact["schema"] == BADGE_SCHEMA
    assert artifact["receipt_id"] == badge["receipt_id"]


# --- Task 1 / AC3-AC4: compose_delivery carries the research output, verdict, badge, order ref --


def test_compose_delivery_carries_output_verdict_badge_and_order_ref():
    deliverable = _real_deliverable()
    artifact = extract_verified_artifact(
        deliverable, content_hash="0xc", deliver_tx_hash="0xabc123", order_id="ord-9"
    )
    composed = compose_delivery(
        research_output="The Eiffel Tower is in Paris.",
        verified_artifact=artifact,
        proov_order_id="ord-9",
    )
    assert composed["research_output"] == "The Eiffel Tower is in Paris."
    assert composed["verified"] is True
    assert composed["verdict"] == "pass"
    assert composed["confidence"] == artifact["confidence"]
    assert composed["verified_by_proov"] is artifact
    assert composed["proov_order"]["order_id"] == "ord-9"
    assert composed["proov_order"]["explorer_url"] == "https://basescan.org/tx/0xabc123"
    # The companion's own delivery must be JSON-serialisable (it prints / ships it).
    assert json.loads(json.dumps(composed)) == composed


def test_compose_delivery_in_band_badge_has_no_explorer_url():
    deliverable = _real_deliverable()
    artifact = extract_verified_artifact(deliverable)  # in-band, anchor=None
    composed = compose_delivery(
        research_output="x", verified_artifact=artifact, proov_order_id="ord-2"
    )
    assert composed["verified"] is True
    assert composed["proov_order"]["explorer_url"] is None


def test_compose_delivery_degrades_to_unverified_on_missing_artifact():
    # No artifact (or an error signal) → an honest unverified composition, never an exception.
    for bad in (None, {"error": "no_receipt", "reason": "x"}):
        composed = compose_delivery(
            research_output="some research", verified_artifact=bad, proov_order_id=None
        )
        assert composed["verified"] is False
        assert composed["verdict"] is None
        assert composed["confidence"] is None
        assert composed["research_output"] == "some research"
        assert composed["verified_by_proov"] == bad
        assert json.loads(json.dumps(composed)) == composed


def test_compose_delivery_failing_verdict_is_not_verified():
    # `verified` means the output PASSED: a real badge with a non-"pass" verdict must surface
    # verified=False while still honestly carrying the verdict + the attached badge.
    fail_report = Report(
        verdict=Verdict(
            label="fail",
            confidence=0.4,
            claims_total=1,
            supported=0,
            unsupported=1,
            unverifiable=0,
        ),
        findings=(),
        citations=(),
        model="gemini-2.5-flash",
    )
    deliverable = build_deliverable(None, "quick", output_text="A false claim.", report=fail_report)
    artifact = extract_verified_artifact(
        deliverable, content_hash="0xc", deliver_tx_hash="0xabc"
    )
    assert artifact["schema"] == BADGE_SCHEMA  # a genuine badge IS attached
    composed = compose_delivery(
        research_output="A false claim.", verified_artifact=artifact, proov_order_id="ord-f"
    )
    assert composed["verified"] is False  # …but the output did not pass
    assert composed["verdict"] == "fail"  # the real verdict is still surfaced
    assert composed["verified_by_proov"] is artifact  # badge still attached for traceability
    assert json.loads(json.dumps(composed)) == composed


def test_full_offline_composition_pipeline_no_socket():
    # End-to-end on the pure core: make output → (would place order) → extract → compose.
    output = make_research_output()
    payload = build_proov_input(output)
    assert payload["mode"] == "quick"
    deliverable = _real_deliverable(output_text=output)
    artifact = extract_verified_artifact(deliverable, content_hash="0xc", deliver_tx_hash="0xt")
    composed = compose_delivery(research_output=output, verified_artifact=artifact, proov_order_id="o")
    assert composed["verified"] is True
    assert composed["research_output"] == output

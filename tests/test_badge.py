"""Tests for the pure "Verified by Proov" artifact builder (Story 1.6).

`proov.badge` is pure and SDK-agnostic — straight unit tests with a hand-built sample
`receipt` dict (no SDK, no real hashing). We assert the *derivation* (what the artifact
copies from the receipt, how `receipt_id` is chosen, the anchor/explorer shape), not hash
values.
"""

from __future__ import annotations

import json

from proov.badge import (
    BADGE_SCHEMA,
    ISSUER,
    build_anchor,
    build_verified_artifact,
    explorer_tx_url,
)

# Hand-built sample `receipt` — exactly the eight keys `build_receipt` emits (proov.receipt).
def _sample_receipt() -> dict:
    return {
        "output_hash": "0xoutput",
        "report_hash": "0xreport",
        "verdict": "unverifiable",
        "confidence": 0.0,
        "model": "stub-no-engine",
        "version": "0.1.0",
        "timestamp": "2026-06-21T00:00:00+00:00",
        "anchor_ref": {
            "chain": "base-mainnet",
            "mechanism": "cap-deliver-keccak256",
            "anchor_field": "content_hash",
        },
    }


def test_in_band_artifact_has_issuer_schema_and_no_anchor():
    artifact = build_verified_artifact(_sample_receipt())
    assert artifact["issuer"] == ISSUER == "Proov"
    assert artifact["schema"] == BADGE_SCHEMA == "proov.verified-by-proov.v1"
    # In-band (pre-delivery): no concrete on-chain anchor yet.
    assert artifact["anchor"] is None


def test_in_band_receipt_id_is_report_hash():
    receipt = _sample_receipt()
    artifact = build_verified_artifact(receipt)
    # Pre-delivery the tx/content_hash are unknown → the stable id is report_hash.
    assert artifact["receipt_id"] == receipt["report_hash"]


def test_artifact_mirrors_the_receipt_identity():
    receipt = _sample_receipt()
    artifact = build_verified_artifact(receipt)
    for key in ("verdict", "confidence", "model", "version", "timestamp",
                "output_hash", "report_hash"):
        assert artifact[key] == receipt[key]
    assert artifact["anchor_ref"] == receipt["anchor_ref"]


def test_artifact_carries_a_verify_block():
    artifact = build_verified_artifact(_sample_receipt())
    verify = artifact["verify"]
    assert "content_hash" in verify["rule"]  # the keccak256 re-canonicalisation rule
    assert "README" in verify["procedure"]


def test_artifact_is_json_serialisable():
    artifact = build_verified_artifact(_sample_receipt())
    assert json.loads(json.dumps(artifact)) == artifact


def test_anchor_ref_is_copied_not_aliased():
    receipt = _sample_receipt()
    artifact = build_verified_artifact(receipt)
    # Mutating the artifact's copy must NOT mutate the source receipt's anchor_ref.
    artifact["anchor_ref"]["chain"] = "tampered"
    assert receipt["anchor_ref"]["chain"] == "base-mainnet"


def test_post_delivery_artifact_receipt_id_is_content_hash():
    receipt = _sample_receipt()
    anchor = build_anchor(
        order_id="ord-1",
        content_hash="0xcontent",
        deliver_tx_hash="0xdeadbeef",
        delivery_id="dlv-1",
    )
    artifact = build_verified_artifact(receipt, anchor=anchor)
    # Once anchored, the on-chain content_hash is the canonical receipt id.
    assert artifact["receipt_id"] == "0xcontent"
    assert artifact["anchor"]["content_hash"] == "0xcontent"
    assert artifact["anchor"]["deliver_tx_hash"] == "0xdeadbeef"
    assert artifact["anchor"]["delivery_id"] == "dlv-1"
    assert artifact["anchor"]["chain"] == "base-mainnet"


def test_build_anchor_explorer_url_shape():
    anchor = build_anchor(order_id="o", content_hash="0xc", deliver_tx_hash="0xabc123")
    assert anchor["explorer_url"] == "https://basescan.org/tx/0xabc123"
    assert anchor["explorer_url"].endswith("/tx/0xabc123")


def test_build_anchor_tolerates_missing_fields():
    # A missing content_hash / tx_hash is recorded as None, never a crash.
    anchor = build_anchor(order_id="o", content_hash=None)
    assert anchor["content_hash"] is None
    assert anchor["deliver_tx_hash"] is None
    assert anchor["explorer_url"] is None


def test_explorer_tx_url_returns_none_for_falsy_hash():
    assert explorer_tx_url("") is None
    assert explorer_tx_url(None) is None
    assert explorer_tx_url("0xabc") == "https://basescan.org/tx/0xabc"


# --- Story 4.2 Task 4: the FR16 seam is hardened for direct consumer (companion) use ----------


def test_build_verified_artifact_complete_receipt_bytes_unchanged():
    # Regression: the hardening must NOT shift the artifact a COMPLETE receipt produces — the
    # report_hash/receipt_id are hashed (Story 1.4) and any change would break re-verification.
    receipt = _sample_receipt()
    artifact = build_verified_artifact(receipt)
    assert artifact["receipt_id"] == receipt["report_hash"]
    assert artifact["anchor_ref"] == receipt["anchor_ref"]
    for key in ("version", "verdict", "confidence", "model", "timestamp",
                "output_hash", "report_hash"):
        assert artifact[key] == receipt[key]


def test_build_verified_artifact_tolerates_partial_receipt():
    # A partial receipt (missing version/anchor_ref) must yield a sensible artifact, never crash —
    # the FR16 seam is now invoked directly by proov.companion.extract_verified_artifact.
    partial = {"report_hash": "0xpartial", "verdict": "pass"}
    artifact = build_verified_artifact(partial)
    assert artifact["report_hash"] == "0xpartial"
    assert artifact["verdict"] == "pass"
    assert artifact["version"] is None  # missing field → None, not KeyError
    assert artifact["anchor_ref"] is None  # missing/non-dict anchor_ref → None, not a crash
    assert artifact["receipt_id"] == "0xpartial"
    assert artifact["schema"] == BADGE_SCHEMA  # still a recognisable badge


def test_build_verified_artifact_tolerates_non_dict_anchor_ref_and_anchor():
    # A non-dict anchor_ref / a non-dict anchor argument must not raise.
    receipt = _sample_receipt()
    receipt["anchor_ref"] = "not-a-dict"  # corrupt shape
    artifact = build_verified_artifact(receipt, anchor="not-a-dict")  # type: ignore[arg-type]
    assert artifact["anchor_ref"] is None
    assert artifact["anchor"] is None
    # No live content_hash (anchor was ignored) → falls back to report_hash.
    assert artifact["receipt_id"] == receipt["report_hash"]


def test_build_verified_artifact_empty_receipt_does_not_crash():
    artifact = build_verified_artifact({})
    assert artifact["schema"] == BADGE_SCHEMA
    assert artifact["receipt_id"] is None  # nothing to anchor to, but no exception

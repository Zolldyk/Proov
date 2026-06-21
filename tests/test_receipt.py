"""Tests for the pure receipt helper (`proov.receipt`) — no socket, no SDK, no platform.

Covers the three load-bearing facts: real keccak256 (known-answer vector, NOT SHA3-256),
deterministic canonical JSON, and the receipt shape (eight keys, reproducible hashes).
"""

from __future__ import annotations

import hashlib
import json

from proov.receipt import build_receipt, canonical_json, keccak256_hex

# Standard Ethereum keccak256 of the empty input. If a run yields the SHA3-256 empty
# vector (a7ffc6f8bf1ed766…) instead, the wrong algorithm is wired up.
_KECCAK_EMPTY = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"

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


def test_keccak256_empty_known_answer_vector():
    # Proves we have Ethereum keccak256, not NIST SHA3-256.
    assert keccak256_hex(b"") == _KECCAK_EMPTY


def test_keccak256_is_not_sha3_256():
    # The two algorithms must disagree on a non-trivial input (sanity guard).
    data = b"proov"
    assert keccak256_hex(data) != "0x" + hashlib.sha3_256(data).hexdigest()


def test_keccak256_is_0x_prefixed_64_hex():
    h = keccak256_hex(b"anything")
    assert h.startswith("0x")
    assert len(h) == 66  # "0x" + 64 hex chars (32 bytes)
    int(h, 16)  # parses as hex


def test_canonical_json_is_stable_across_key_order():
    a = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    b = {"a": 2, "nested": {"x": 2, "y": 1}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    # Compact + sorted, no whitespace.
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_preserves_unicode_raw():
    # ensure_ascii=False keeps the em-dash a single raw char (not —).
    s = canonical_json({"summary": "a — b"})
    assert "—" in s
    assert "\\u2014" not in s


def test_build_receipt_has_all_eight_keys():
    receipt = build_receipt(
        output_text="the claim under test",
        report_body={"verdict": "unverifiable", "confidence": 0.0},
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
        timestamp="2026-06-21T00:00:00+00:00",
    )
    assert set(receipt.keys()) == _RECEIPT_KEYS


def test_build_receipt_hashes_are_0x_64hex():
    receipt = build_receipt(
        output_text="hello",
        report_body={"verdict": "unverifiable"},
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    for key in ("output_hash", "report_hash"):
        assert receipt[key].startswith("0x") and len(receipt[key]) == 66


def test_build_receipt_output_hash_is_keccak_of_input_text():
    output_text = "Paris is the capital of France."
    receipt = build_receipt(
        output_text=output_text,
        report_body={"verdict": "unverifiable"},
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    assert receipt["output_hash"] == keccak256_hex(output_text.encode("utf-8"))


def test_build_receipt_report_hash_reproducible_from_canonical_body():
    report_body = {
        "verdict": "unverifiable",
        "confidence": 0.0,
        "summary": "stub — engine pending",
        "claims": [],
    }
    receipt = build_receipt(
        output_text="x",
        report_body=report_body,
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    # Any party can recompute report_hash from the canonical body.
    expected = keccak256_hex(canonical_json(report_body).encode("utf-8"))
    assert receipt["report_hash"] == expected


def test_build_receipt_defaults_timestamp_to_iso8601_utc():
    receipt = build_receipt(
        output_text="x",
        report_body={},
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    # Default is timezone-aware UTC ISO-8601 (parseable, ends in +00:00).
    from datetime import datetime

    parsed = datetime.fromisoformat(receipt["timestamp"])
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_build_receipt_is_json_serialisable():
    receipt = build_receipt(
        output_text="x",
        report_body={"verdict": "unverifiable"},
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    assert json.loads(json.dumps(receipt)) == receipt


def test_build_receipt_anchor_ref_is_descriptor_not_tx():
    receipt = build_receipt(
        output_text="x",
        report_body={},
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    anchor = receipt["anchor_ref"]
    # A stable descriptor of where the anchor lives — never a tx hash / content_hash.
    assert anchor["chain"] == "base-mainnet"
    assert anchor["mechanism"] == "cap-deliver-keccak256"
    assert anchor["anchor_field"] == "content_hash"
    assert "tx" not in json.dumps(anchor).lower()


def test_report_hash_matches_independent_known_answer():
    # Pin the EXACT canonical bytes + digest with an oracle that does NOT route through
    # `canonical_json` — so flipping sort_keys / separators / ensure_ascii breaks this
    # (the circular `test_..._reproducible_from_canonical_body` above could not catch that).
    report_body = {
        "verdict": "unverifiable",
        "confidence": 0.0,
        "summary": "stub",
        "claims": [],
    }
    expected_canon = '{"claims":[],"confidence":0.0,"summary":"stub","verdict":"unverifiable"}'
    expected_report_hash = (
        "0xff4b3139888977a17c802a82ed0ef2e0f6d612c3d79daa0f498fa72bc8230810"
    )
    # Guard the serialisation contract itself, then the resulting receipt hash.
    assert canonical_json(report_body) == expected_canon
    receipt = build_receipt(
        output_text="x",
        report_body=report_body,
        verdict="unverifiable",
        confidence=0.0,
        model="stub-no-engine",
        version="0.1.0",
    )
    assert receipt["report_hash"] == expected_report_hash


def test_canonical_json_rejects_non_finite_floats():
    import pytest

    # NaN/Infinity are not valid JSON and would silently make the anchor un-reproducible
    # for strict (non-Python) verifiers — canonical_json must refuse them, not emit `NaN`.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"confidence": bad})

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
    render_badge_html,
    render_badge_markdown,
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


# --- Story 4.3: the badge RENDERER (visible, embeddable HTML + Markdown) -----------------------

_AFFIRMATIVE = "✓ Verified by Proov"


def _tx_bearing_pass_artifact() -> dict:
    receipt = _sample_receipt()
    receipt["verdict"] = "pass"
    anchor = build_anchor(
        order_id="ord-1",
        content_hash="0xcontenthash",
        deliver_tx_hash="0xdeadbeef",
        delivery_id="dlv-1",
    )
    return build_verified_artifact(receipt, anchor=anchor)


def _in_band_pass_artifact() -> dict:
    receipt = _sample_receipt()
    receipt["verdict"] = "pass"
    return build_verified_artifact(receipt)  # anchor=None → preview form


# AC2/AC3: a tx-bearing pass renders affirmative WITH the BaseScan link + content_hash receipt id.
def test_render_html_tx_bearing_pass_is_affirmative_with_basescan_link():
    html_str = render_badge_html(_tx_bearing_pass_artifact())
    assert _AFFIRMATIVE in html_str
    assert "https://basescan.org/tx/0xdeadbeef" in html_str
    assert "0xcontenthash" in html_str  # receipt_id == content_hash when anchored


def test_render_markdown_tx_bearing_pass_is_affirmative_with_basescan_link():
    md = render_badge_markdown(_tx_bearing_pass_artifact())
    assert _AFFIRMATIVE in md
    assert "basescan.org/tx/0xdeadbeef" in md
    assert "0xcontenthash" in md


# AC3: the in-band/preview form (anchor=null) says "preview", shows report_hash, NO tx link.
def test_render_in_band_preview_says_preview_and_has_no_tx_link():
    for render in (render_badge_html, render_badge_markdown):
        out = render(_in_band_pass_artifact())
        assert "preview" in out.lower()
        assert "not anchored" in out.lower()
        assert "basescan.org" not in out  # never fabricate a proof link
        assert "0xreport" in out  # receipt_id == report_hash (pre-delivery stable)


# AC2: a non-pass verdict NEVER renders the affirmative green form; the real verdict IS surfaced.
def test_render_non_pass_verdict_is_not_affirmative():
    for verdict in ("fail", "partial", "unverifiable"):
        receipt = _sample_receipt()
        receipt["verdict"] = verdict
        artifact = build_verified_artifact(receipt)
        for out in (render_badge_html(artifact), render_badge_markdown(artifact)):
            assert _AFFIRMATIVE not in out
            assert verdict in out


# AC8: a None / error / non-badge / partial artifact degrades to honest "unverified", never raises.
def test_render_degrades_on_bad_artifact_without_raising():
    for bad in (None, {"error": "no_receipt", "reason": "x"}, {"schema": "other"}, "nope", {}):
        html_str = render_badge_html(bad)  # type: ignore[arg-type]
        md = render_badge_markdown(bad)  # type: ignore[arg-type]
        assert isinstance(html_str, str) and isinstance(md, str)
        assert _AFFIRMATIVE not in html_str and _AFFIRMATIVE not in md
        assert "unverified" in html_str.lower()
        assert "unverified" in md.lower()
        assert "basescan.org" not in html_str  # no fabricated proof


# AC4: the HTML renderer html.escapes every interpolated value (reflected-XSS guard).
def test_render_html_escapes_xss_in_values():
    receipt = _sample_receipt()
    receipt["verdict"] = '<script>alert("x")</script>'
    receipt["model"] = '<img src=x onerror=alert(1)>'
    html_str = render_badge_html(build_verified_artifact(receipt))
    assert "<script>alert" not in html_str
    assert "<img src=x" not in html_str
    assert "&lt;script&gt;" in html_str


# AC4: the Markdown renderer neutralises link/markup-breaking characters in interpolated values.
def test_render_markdown_neutralises_link_breaking_chars():
    receipt = _sample_receipt()
    receipt["verdict"] = "pass](http://evil.example)"  # tries to break out of a markdown link
    md = render_badge_markdown(build_verified_artifact(receipt))
    assert "](http://evil.example)" not in md  # the injected link syntax is escaped


def test_render_badge_html_and_markdown_are_pure_strings():
    artifact = _tx_bearing_pass_artifact()
    assert isinstance(render_badge_html(artifact), str)
    assert isinstance(render_badge_markdown(artifact), str)

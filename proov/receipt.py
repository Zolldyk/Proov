"""Real, independently-reproducible on-chain receipt (Story 1.4).

Pure and SDK-agnostic (no `croo` import, no I/O): given the submitted input text and the
report body, build the PRD §6 `receipt` object with **real Ethereum keccak256** hashes.

Three load-bearing facts, all empirically confirmed against the Story 1.3 live anchor
`0xadedb261…34b6b3` (see `scripts/probe_anchor.py` / Dev Notes):

1. **keccak256 ≠ SHA3-256.** Base is an Ethereum L2; CAP anchors with Ethereum keccak256
   (pre-NIST). `hashlib.sha3_256` produces a *different* digest. We use pycryptodome's
   `keccak`. Sanity vector: `keccak256(b"") == c5d2460186f7233c…d85a470` (if you instead
   see `a7ffc6f8bf1ed766…` you have SHA3-256 — the wrong algorithm).

2. **The anchor is keccak256 of the EXACT bytes the provider POSTs.** The CAP backend
   does NOT re-hash a canonical form of its own — it hashes the `deliverableSchema`
   string verbatim. `get_delivery` returns a *re-serialised* copy (keys reordered,
   unicode decoded), so hashing the returned string verbatim does NOT reproduce
   `content_hash`. The provider therefore POSTs `canonical_json(...)` and a verifier
   reproduces the anchor as `keccak256(canonical_json(json.loads(deliverable_schema)))`
   — `canonical_json` is order-independent, so the server's reorder washes out.

3. **Receipt circularity.** `report_hash` hashes the report body WITHOUT the `receipt`
   key (a structure cannot contain the hash of itself), and `anchor_ref` is a stable
   descriptor of *where* the anchor lives — never the deliver tx / `content_hash`, which
   exist only after delivery and would change the very bytes being hashed. The tx-bearing
   reference is the post-delivery "Verified by Proov" artifact (Story 1.6).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from Crypto.Hash import keccak

# Stable, pre-known descriptor of where/how the anchor lives. NOT a tx id — see the
# module docstring (receipt circularity). The post-delivery tx reference is Story 1.6.
ANCHOR_REF = {
    "chain": "base-mainnet",
    "mechanism": "cap-deliver-keccak256",
    "anchor_field": "content_hash",
}


def keccak256_hex(data: bytes) -> str:
    """Return `"0x"` + the 64-hex-char Ethereum keccak256 of `data`.

    NEVER use `hashlib.sha3_256` here — NIST SHA3-256 is a different digest and will not
    match CAP's on-chain anchor.
    """
    return "0x" + keccak.new(digest_bits=256, data=data).hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialisation any party can reproduce byte-for-byte.

    Sorted keys + no whitespace + raw unicode (`ensure_ascii=False`). Order-independent,
    so it is stable across the CAP backend's storage re-ordering — that is what makes the
    on-chain anchor reproducible from the (re-serialised) `get_delivery` payload.

    `allow_nan=False`: `NaN`/`Infinity` are not valid JSON (RFC-8259) and strict non-Python
    verifiers reject them, which would silently make the anchor un-reproducible — so we fail
    loudly here instead of hashing bytes no other party can reproduce.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def build_receipt(
    *,
    output_text: str,
    report_body: dict,
    verdict: str,
    confidence: float,
    model: str,
    version: str,
    timestamp: str | None = None,
) -> dict:
    """Build the PRD §6 `receipt` object (pure — no I/O, no `croo` import).

    `output_hash` = keccak256 of the exact UTF-8 bytes of the submitted `output_text`
    (the input is fixed before delivery, so this is non-circular). `report_hash` =
    keccak256 of `canonical_json(report_body)`, where `report_body` is the deliverable
    MINUS the `receipt` key (circularity — see module docstring). `timestamp` defaults to
    the current ISO-8601 UTC instant.
    """
    return {
        "output_hash": keccak256_hex(output_text.encode("utf-8")),
        "report_hash": keccak256_hex(canonical_json(report_body).encode("utf-8")),
        "verdict": verdict,
        "confidence": confidence,
        "model": model,
        "version": version,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "anchor_ref": dict(ANCHOR_REF),
    }

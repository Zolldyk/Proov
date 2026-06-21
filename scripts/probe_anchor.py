"""Story 1.4 / Task 1 — reproduce the CAP on-chain hash rule (AC3).

MANUAL probe (NOT in the automated `pytest` suite — it hits the live platform via
`get_delivery`, read-only, no money spent). It fetches a delivery and reproduces the
on-chain `content_hash` with **Ethereum keccak256** (NOT NIST SHA3-256).

Usage:
    .venv/bin/python scripts/probe_anchor.py [order_id]

Empirically confirmed rule (see README "Verify a receipt independently"):
    content_hash = keccak256( exact UTF-8 bytes the provider POSTed as deliverableSchema )

Two regimes, because `get_delivery` returns a *re-serialised* copy (keys reordered,
unicode decoded) — so hashing the returned string verbatim never matches:

  • Orders delivered from Story 1.4 on POST **canonical JSON** (sorted/compact/raw-unicode),
    which is order-independent — so a verifier reproduces the anchor by re-canonicalising
    the returned object: keccak256(canonical_json(json.loads(deliverable_schema))).

  • The pre-1.4 reference order `2c4ac135-…` was delivered with Python's default
    `json.dumps` (spaced separators, ensure_ascii=True, insertion order) — its anchor
    reproduces only from those exact original bytes, reconstructed below from the known
    historical payload. (This is what originally pinned the rule down.)
"""

from __future__ import annotations

import asyncio
import json
import sys

from Crypto.Hash import keccak

from proov.config import AppConfig

# Pre-1.4 reference order + its known on-chain anchor (README "First live order").
_LEGACY_ORDER = "2c4ac135-ef8a-4162-9396-4088cfb06854"
_LEGACY_CONTENT_HASH = (
    "0xadedb261d3ca8bf65554f2b3a7e775d9e0f95b33f660bfad6611bf072434b6b3"
)
# The exact payload that order delivered (Story 1.3 stub: receipt was the `{}` placeholder),
# in its original key-insertion order. Re-`json.dumps`-ing this with Python defaults
# reproduces the byte-for-byte string that was POSTed and therefore the on-chain anchor.
_LEGACY_PAYLOAD = {
    "verdict": "unverifiable",
    "confidence": 0.0,
    "summary": (
        "Stub deliverable — Proov's verification engine lands in Epic 2. "
        "No claims were extracted, retrieved, or judged for this order."
    ),
    "claims": [],
    "citations_checked": [],
    "stats": {"tier": "quick"},
    "receipt": {},
    "disclaimer": (
        "Best-effort automated verification. Proov is not a substitute for human review; "
        "verdicts may be incomplete or wrong. (FR13)"
    ),
}


def keccak256_hex(data: bytes) -> str:
    return "0x" + keccak.new(digest_bits=256, data=data).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _main() -> int:
    order_id = sys.argv[1] if len(sys.argv) > 1 else _LEGACY_ORDER
    try:
        cfg = AppConfig.from_env()
    except Exception as exc:  # missing CROO_API_KEY / CROO_API_URL etc.
        print(f"config error: {exc}\nSet CROO_API_KEY / CROO_API_URL / CROO_WS_URL (.env).")
        return 2

    # keccak sanity: prove we have Ethereum keccak256, not SHA3-256, before anything else.
    empty = keccak256_hex(b"")
    print(f"keccak256('')       : {empty}")
    assert empty == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470", (
        "WRONG ALGORITHM — got SHA3-256, expected Ethereum keccak256"
    )

    from croo import AgentClient, Config

    client = AgentClient(
        Config(base_url=cfg.api_url, ws_url=cfg.ws_url),
        sdk_key=cfg.api_key,
    )
    try:
        delivery = await client.get_delivery(order_id)
    finally:
        await client.close()

    schema = delivery.deliverable_schema
    content_hash = (delivery.content_hash or "").lower()
    print(f"order_id            : {order_id}")
    print(f"content_hash (chain): {content_hash}")
    print(f"deliverable_schema  : {schema!r}")

    # Regime 1 — generic (Story 1.4+): re-canonicalise the returned object.
    try:
        returned_obj = json.loads(schema)
    except (ValueError, TypeError) as exc:
        print(f"\nNO MATCH — deliverable_schema is not parseable JSON ({exc}); "
              "inspect the returned schema manually (delivered as text, not schema?).")
        return 1
    canon_hash = keccak256_hex(canonical_json(returned_obj).encode("utf-8"))
    canon_ok = canon_hash == content_hash
    print(f"  [{'MATCH' if canon_ok else '   x '}] keccak256(canonical_json(returned)) = {canon_hash}")

    if canon_ok:
        print("\nCONFIRMED: anchor = keccak256(canonical_json(deliverable)) — a verifier "
              "reproduces it from get_delivery by re-canonicalising. (Story 1.4 contract.)")
        return 0

    # Regime 2 — legacy reference order: reproduce from the original POSTed bytes.
    if order_id == _LEGACY_ORDER:
        legacy_bytes = json.dumps(_LEGACY_PAYLOAD)  # Python defaults = the original POST
        legacy_hash = keccak256_hex(legacy_bytes.encode("utf-8"))
        legacy_ok = legacy_hash == content_hash and content_hash == _LEGACY_CONTENT_HASH
        print(f"  [{'MATCH' if legacy_ok else '   x '}] keccak256(original json.dumps bytes) = {legacy_hash}")
        if legacy_ok:
            print("\nCONFIRMED: anchor = keccak256(exact POSTed bytes). This pre-1.4 order "
                  "was delivered with non-canonical json.dumps, so it reproduces only from "
                  "those original bytes; Story 1.4+ delivers canonical JSON (Regime 1).")
            return 0

    print("\nNO MATCH — inspect the returned schema manually.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

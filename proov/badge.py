"""Pure "Verified by Proov" artifact builder (Story 1.6).

The reusable proof-of-verification artifact (FR16). Given a deliverable's already-built
`receipt` dict (+ an optional concrete on-chain `anchor`), produce a self-contained,
JSON-serialisable "Verified by Proov" payload that anyone can trace back to the anchored
receipt. The Report + on-chain receipt are the reusable currency (architecture §5/§7); this
turns them into a portable badge a caller agent can attach to its own delivery.

Pure and SDK-agnostic, exactly like `proov.receipt` / `proov.services` / `proov.validation`:
NO `croo` import, NO network, NO logging, NO I/O. The builder only *reads* an already-built
receipt — it never hashes anything (it does not import `proov.receipt` for hashing).

Two forms (see the README "'Verified by Proov' artifact" section and Story 1.4 circularity):
- **In-band** (`build_verified_artifact(receipt)`, `anchor=None`): the badge embedded as a
  sibling of `receipt` inside every deliverable. `receipt_id = report_hash` (pre-delivery
  stable; the deliver tx / `content_hash` are not yet known and must NOT be embedded — the
  same circularity that kept them out of the receipt in 1.4).
- **Post-delivery** (`anchor=build_anchor(...)`): assembled by the provider after
  `deliver_order` returns, carrying the now-known `content_hash` / `deliver_tx_hash` /
  `explorer_url`. `receipt_id = content_hash` (the on-chain anchor is the canonical id once
  known). This is the surface the Epic 4 caller attaches to its own delivery.
"""

from __future__ import annotations

from typing import Any

# Stable artifact identity. `schema` lets any third party recognise + version the payload.
BADGE_SCHEMA = "proov.verified-by-proov.v1"
ISSUER = "Proov"
DEFAULT_CHAIN = "base-mainnet"

# How a third party re-verifies — the Story 1.4 keccak256 re-canonicalisation rule + a
# README pointer. Carried in the artifact so the proof is self-describing.
_VERIFY_RULE = (
    "keccak256(canonical_json(json.loads(get_delivery(order_id).deliverable_schema))) "
    "== content_hash"
)
_VERIFY_PROCEDURE = "README#verify-a-receipt-independently-story-14"


def explorer_tx_url(tx_hash: str | None, chain: str = DEFAULT_CHAIN) -> str | None:
    """Return the Base explorer URL for `tx_hash`, or `None` if `tx_hash` is falsy.

    Defensive: a missing tx hash yields `None` rather than a broken `…/tx/None` URL. Base
    mainnet only today (the project has no testnet — architecture §5/decision 6).
    """
    if not tx_hash:
        return None
    return f"https://basescan.org/tx/{tx_hash}"


def build_anchor(
    *,
    order_id: str | None,
    content_hash: str | None,
    deliver_tx_hash: str | None = None,
    delivery_id: str | None = None,
    chain: str = DEFAULT_CHAIN,
) -> dict:
    """Build the concrete on-chain `anchor` block for the post-delivery artifact.

    Carries the now-known on-chain references: `content_hash` (the anchor / receipt id),
    `deliver_tx_hash`, `delivery_id`, `chain`, and an `explorer_url` for the deliver tx.
    Tolerates `None` fields — a missing `content_hash`/`tx_hash` is recorded as `None`,
    never a crash (the artifact assembly must not fail after a successful anchor).
    """
    return {
        "order_id": order_id,
        "content_hash": content_hash,
        "deliver_tx_hash": deliver_tx_hash,
        "delivery_id": delivery_id,
        "chain": chain,
        "explorer_url": explorer_tx_url(deliver_tx_hash, chain),
    }


def build_verified_artifact(receipt: dict, *, anchor: dict | None = None) -> dict:
    """Build the "Verified by Proov" artifact derived from a deliverable's `receipt`.

    Pure: the whole payload is derived from `receipt` (+ optional `anchor`) — no I/O, no
    `croo` import, no hashing. The result is a fresh `json.dumps`-serialisable dict that
    shares no mutable references with `receipt` (`anchor_ref` is copied, not aliased).

    `receipt_id` is the pre-delivery-stable `report_hash` when there is no `anchor`
    (in-band form), and the on-chain `content_hash` once the `anchor` is known
    (post-delivery form). The `verify` block tells a third party exactly how to re-verify.
    """
    return {
        "issuer": ISSUER,
        "schema": BADGE_SCHEMA,
        "version": receipt["version"],
        "verdict": receipt["verdict"],
        "confidence": receipt["confidence"],
        "model": receipt["model"],
        "timestamp": receipt["timestamp"],
        "output_hash": receipt["output_hash"],
        "report_hash": receipt["report_hash"],
        # Copy — never alias the receipt's nested dict (mutating one must not touch the other).
        "anchor_ref": dict(receipt["anchor_ref"]),
        # Copy — never alias the caller's anchor dict (consistency with anchor_ref above; a
        # caller that reuses/mutates its anchor must not mutate this finalised artifact).
        "anchor": dict(anchor) if anchor else None,
        # The on-chain content_hash once it is concretely known, else the pre-delivery-stable
        # report_hash. Guard on the *value* (not just dict-presence) so an anchor that lacks
        # content_hash (or carries None) falls back to report_hash rather than yielding a
        # null receipt_id.
        "receipt_id": (anchor.get("content_hash") if anchor else None) or receipt["report_hash"],
        "verify": {"rule": _VERIFY_RULE, "procedure": _VERIFY_PROCEDURE},
    }


# Convenience for callers that want canonical bytes of the artifact without reaching for
# `proov.receipt` (no hashing happens here; this only re-exports the serialiser).
def _canonical_json(obj: Any) -> str:  # pragma: no cover - thin re-export
    from .receipt import canonical_json

    return canonical_json(obj)

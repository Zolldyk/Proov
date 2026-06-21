"""Deliverable builder — PRD §6 report body + a REAL on-chain receipt (Story 1.4/1.5).

Builds a JSON-serialisable dict matching the PRD §6 deliverable contract. As of Story 1.4
the `receipt` is real: `output_hash`/`report_hash` are genuine Ethereum keccak256 hashes,
plus producing `model`/`version`/`timestamp` and a stable `anchor_ref` descriptor (see
`proov.receipt`). The *verdict/claims remain an explicit stub* (`"unverifiable"`, `[]`)
until the Epic 2 engine lands — only the receipt became real here. The engine will later
pass the real `output_text` and `model`.

Story 1.5 adds `build_graceful_deliverable`: the **failure contract** — when the
verification step raises an internal error, the provider delivers an honest
`unverifiable`/`partial` report (degrade, don't drop — NFR3) rather than letting the order
fall to an SLA timeout. The *verdict logic* is still Epic 2; the degrade builder is the seam
the real `verify()` plugs into. It reuses `build_receipt` exactly, so a graceful deliverable
carries a real, reproducible receipt too.

As of Story 1.6 every deliverable also carries a top-level `verified_by_proov` in-band
badge — the "Verified by Proov" artifact (see `proov.badge`) with `anchor: null` and
`receipt_id = report_hash` — so reading the delivered JSON yields the reusable
proof-of-verification artifact (FR16). It is a **sibling** of `receipt`, added *after* the
receipt is computed, so it never feeds `report_hash` (non-circular) and the happy-path
receipt bytes are unchanged. A verifier reproducing `report_hash` from the delivered object
must strip **both** `receipt` and `verified_by_proov` before re-canonicalising. The
tx-bearing form (concrete on-chain `anchor`) is assembled post-delivery by the provider.

Pure and SDK-agnostic (no `croo` import, no I/O): the provider serialises the returned
dict with `canonical_json` so the on-chain anchor is reproducible (Story 1.4 contract).
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .badge import build_verified_artifact
from .receipt import build_receipt

# Clear, honest stub copy so anyone inspecting a delivered order knows the engine is
# pending rather than mistaking the stub for a real verdict.
_STUB_SUMMARY = (
    "Stub deliverable — Proov's verification engine lands in Epic 2. "
    "No claims were extracted, retrieved, or judged for this order."
)
_DISCLAIMER = (
    "Best-effort automated verification. Proov is not a substitute for human review; "
    "verdicts may be incomplete or wrong. (FR13)"
)
# Honest engine id — Epic 2 swaps this for the real LLM model id.
_STUB_MODEL = "stub-no-engine"


def build_stub_deliverable(order: Any, tier: str, *, output_text: str = "") -> dict:
    """Return a PRD §6 deliverable for `order` at `tier`, with a real `receipt`.

    The *report body* (verdict/confidence/summary/claims/citations_checked/stats/
    disclaimer) is still a stub — the Epic 2 engine replaces it. The `receipt` is real:
    `output_hash` = keccak256 of the submitted `output_text`, `report_hash` = keccak256 of
    the canonical-JSON report body **excluding** the receipt (non-circular — see
    `proov.receipt`). `order` is accepted for forward-compatibility (the real engine needs
    it); only `tier` is used today. The returned dict is `json.dumps`-serialisable and
    carries every PRD §6 top-level key.
    """
    # Report body = the deliverable WITHOUT the `receipt` key. `report_hash` is computed
    # over this body so the receipt never hashes a structure containing itself.
    report_body = {
        "verdict": "unverifiable",  # engine pending — never assert pass/fail from a stub
        # Always a float: `0` and `0.0` canonicalise to different bytes (`"0"` vs `"0.0"`),
        # which would change `report_hash`. The Epic 2 engine must keep this a float.
        "confidence": 0.0,
        "summary": _STUB_SUMMARY,
        "claims": [],
        "citations_checked": [],
        "stats": {"tier": tier},
        "disclaimer": _DISCLAIMER,
    }
    receipt = build_receipt(
        output_text=output_text,
        report_body=report_body,
        verdict=report_body["verdict"],
        confidence=report_body["confidence"],
        model=_STUB_MODEL,
        version=__version__,
    )
    # In-band badge: built AFTER the receipt, over the unchanged body — a sibling of
    # `receipt`, so it never perturbs `report_hash` (anchor=None, receipt_id=report_hash).
    badge = build_verified_artifact(receipt)
    return {**report_body, "receipt": receipt, "verified_by_proov": badge}


def build_graceful_deliverable(
    order: Any,
    tier: str,
    *,
    output_text: str = "",
    reason: str,
    verdict: str = "unverifiable",
) -> dict:
    """Return a PRD §6 deliverable for an order whose verification could not complete.

    Same shape + real `receipt` as the happy stub, but honest about the failure: an
    explanatory `summary` that names `reason` (never a stack trace / no secrets),
    `verdict="unverifiable"` (or `"partial"` for Epic-2 partial progress), `confidence`
    the float `0.0` (`0` vs `0.0` canonicalise to different bytes → different `report_hash`),
    and a `stats.degraded` flag so an inspector knows this was a graceful degrade, not a
    real verdict. The order still reaches `completed` with value delivered rather than being
    dropped to an SLA timeout (NFR3). `order` is accepted for forward-compatibility (the
    Epic 2 engine needs it); only `tier` is used today.
    """
    # Summary reflects the verdict: a `partial` carries some judged progress, whereas an
    # `unverifiable` could not judge anything — wording one for the other would contradict
    # the verdict an inspector reads.
    if verdict == "partial":
        summary = (
            "Proov could only partially verify this order "
            f"(reason: {reason}). This is an honest degraded result, delivered rather than "
            "dropped, so the order still completes; verification is incomplete."
        )
    else:
        summary = (
            "Proov could not complete verification for this order "
            f"(reason: {reason}). This is an honest degraded result, delivered rather than "
            "dropped, so the order still completes; no claim was judged."
        )
    report_body = {
        "verdict": verdict,  # "unverifiable" (engine error) or "partial" (partial progress)
        "confidence": 0.0,  # float — keep it 0.0, not 0 (canonical-bytes / report_hash)
        "summary": summary,
        "claims": [],
        "citations_checked": [],
        "stats": {"tier": tier, "degraded": True},
        "disclaimer": _DISCLAIMER,
    }
    receipt = build_receipt(
        output_text=output_text,
        report_body=report_body,
        verdict=report_body["verdict"],
        confidence=report_body["confidence"],
        model=_STUB_MODEL,
        version=__version__,
    )
    # In-band badge sibling (see build_stub_deliverable) — a degraded order is still
    # delivered, so it carries the artifact like any delivered deliverable.
    badge = build_verified_artifact(receipt)
    return {**report_body, "receipt": receipt, "verified_by_proov": badge}

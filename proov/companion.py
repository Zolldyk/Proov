"""Testable core of the companion "Research" caller (Story 4.2).

The first **on-protocol** demo surface: a thin, separate "Research" agent that produces an
output and then **hires Proov** (a real paid Quick Check order over CAP) to verify it *before*
delivering — making the agent-hires-agent (A2A) relationship a **real on-chain order**, not a
mock. This module is the pure, testable composition core; the live CROO buyer wiring lives in
`scripts/research_caller.py` (a thin clone of `scripts/place_test_order.py`).

SDK-agnostic by design (architecture §2: only `[A] provider.py` is CROO-coupled): **NO `croo`
import** and no I/O. It imports only `proov.badge` (the FR16 "Verified by Proov" reuse seam it
consumes from the *buyer* side for the first time). All logic worth testing lives here as pure
functions; the runner is a thin socket/buyer shell — same `proov/` core vs `scripts/` runner
split as Story 4.1's `webdemo.py` + `try_this.py`.

The composition is deliberately **thin** (the point is the A2A composition, not research
quality): it produces a research-style `output`, hands it to Proov, waits for the verdict, then
attaches Proov's on-chain "Verified by Proov" badge to its own delivery. It re-implements NO
verification, claim, judgment, deliverable, receipt, or badge logic — those are owned by the
engine / `deliverable` / `badge` modules.
"""

from __future__ import annotations

from typing import Any

from proov.badge import (
    BADGE_SCHEMA,
    build_anchor,
    build_verified_artifact,
    explorer_tx_url,
)

# A small built-in, Wikipedia-checkable sample output to verify — the same factual sentence the
# proven buyer harness (`scripts/place_test_order.py`) uses, so the live demo yields a meaningful
# verdict. Deliberately thin: this is NOT a real research agent (AC1/AC6).
_SAMPLE_OUTPUT = (
    "The Eiffel Tower is located in Paris, France and was completed in 1889."
)


def make_research_output(topic: str | None = None) -> str:
    """Produce a **thin** research-style output for Proov to verify (AC1, AC6).

    Returns the caller-supplied `topic`/text when it is a non-empty string; otherwise the
    built-in factual sample. There is **no** real research generation here — the demo's point is
    the A2A composition, not research quality, so the core stays $0/offline and deterministic
    (the optional-LLM drafting path is intentionally not wired: the existing `LLMProvider`
    Protocol exposes only `extract_claims`/`judge_claim`, no text-generation seam — adding one
    would exceed the "thin companion" scope and the keyless built-in sample fully satisfies AC6).
    """
    if isinstance(topic, str) and topic.strip():
        return topic
    return _SAMPLE_OUTPUT


def build_proov_input(output_text: str, sources: list[str] | None = None) -> dict:
    """Build the PRD §6 requirements payload Proov expects (AC2/AC3).

    `{"output": output_text, "mode": "quick"}`, plus an optional `sources` list in the §6
    `[{"url": …}]` shape (mirroring `webdemo._parse_sources`; blank/non-string entries dropped,
    and the `sources` key is omitted entirely when empty so the validator's optional-`sources`
    rule never trips on an empty list). The runner JSON-encodes this into
    `NegotiateOrderRequest.requirements`.
    """
    payload: dict[str, Any] = {"output": output_text, "mode": "quick"}
    if sources:
        urls = [{"url": s.strip()} for s in sources if isinstance(s, str) and s.strip()]
        if urls:
            payload["sources"] = urls
    return payload


def extract_verified_artifact(
    deliverable: dict,
    *,
    content_hash: str | None = None,
    deliver_tx_hash: str | None = None,
    order_id: str | None = None,
    delivery_id: str | None = None,
) -> dict | None:
    """The FR16 consumer seam: turn Proov's delivered deliverable into a "Verified by Proov"
    artifact the companion can attach to its own delivery (AC4).

    Reads `deliverable["receipt"]`. When a live on-chain `content_hash` is known (post-delivery),
    it builds the **tx-bearing** artifact via `build_anchor(...)` + `build_verified_artifact(
    receipt, anchor=…)` — the exact surface the provider assembles post-delivery
    (`provider.py:598-608`). With no live anchor (offline test path / pre-funding) it degrades to
    the **in-band** badge already carried by the deliverable (`verified_by_proov`, `anchor:
    null`), or rebuilds it from the receipt. Re-implements no badge logic.

    Defensive (AC8): a missing/partial `receipt`/badge, or any internal error, yields a structured
    `{"error": …}` signal — **never** raises (the badge seam itself is also hardened in Task 4).
    """
    if not isinstance(deliverable, dict):
        return {"error": "no_deliverable", "reason": "deliverable is not a dict"}
    try:
        receipt = deliverable.get("receipt")
        if isinstance(receipt, dict):
            if content_hash:
                # Post-delivery, tx-bearing form: the on-chain anchor is the canonical receipt id.
                anchor = build_anchor(
                    order_id=order_id,
                    content_hash=content_hash,
                    deliver_tx_hash=deliver_tx_hash,
                    delivery_id=delivery_id,
                )
                return build_verified_artifact(receipt, anchor=anchor)
            # No live anchor: prefer the in-band badge the deliverable actually shipped, else
            # rebuild it from the receipt (anchor=None). Copy so the caller can't mutate the
            # deliverable's nested dict by editing the returned artifact.
            inband = deliverable.get("verified_by_proov")
            if isinstance(inband, dict):
                return dict(inband)
            return build_verified_artifact(receipt)
        # No usable receipt — fall back to whatever in-band badge exists; honest error otherwise.
        inband = deliverable.get("verified_by_proov")
        if isinstance(inband, dict):
            return dict(inband)
        return {
            "error": "no_receipt",
            "reason": "deliverable carries neither a receipt nor a verified_by_proov badge",
        }
    except Exception as exc:  # never raise out of the FR16 seam (AC8)
        return {"error": "extract_failed", "reason": str(exc)}


def compose_delivery(
    *,
    research_output: str,
    verified_artifact: dict | None,
    proov_order_id: str | None = None,
) -> dict:
    """The companion's OWN final delivery — verify-before-deliver, with Proov's badge attached.

    A JSON-serialisable dict carrying the `research_output`, the Proov `verdict`/`confidence`
    (read from the artifact), the embedded `verified_by_proov` artifact (FR16), a `verified`
    flag, and a `proov_order` reference (`order_id` + the BaseScan explorer URL for the deliver
    tx). Pure — no I/O. The object a downstream consumer (or Story 4.3's badge render) reads.

    `verified` means the output **passed** Proov verification: it is True only when a genuine
    Proov badge is attached AND its verdict is `"pass"`. A failing/partial/unverifiable verdict —
    or a missing/error artifact — yields `verified=False` so the top-level flag never overstates
    the result; the actual `verdict`/`confidence` and the full badge are always carried alongside
    so a consumer sees the real outcome.

    Degrade path (AC8): a missing/error artifact → an honest **unverified** composition
    (`verified=False`, `verdict=None`), embedding the error signal rather than crashing.
    """
    # A real Proov artifact is recognisable by its badge schema; an `{"error": …}` signal (or
    # None) is not. The badge's verdict/confidence/anchor are still read whenever a badge is
    # present (even on a fail), but `verified` is gated on a *passing* verdict (see docstring).
    has_badge = isinstance(verified_artifact, dict) and verified_artifact.get("schema") == BADGE_SCHEMA

    verdict = verified_artifact.get("verdict") if has_badge else None
    confidence = verified_artifact.get("confidence") if has_badge else None
    verified = has_badge and verdict == "pass"

    explorer_url = None
    if has_badge:
        anchor = verified_artifact.get("anchor")
        if isinstance(anchor, dict):
            explorer_url = anchor.get("explorer_url") or explorer_tx_url(anchor.get("deliver_tx_hash"))

    return {
        "agent": "research-companion",
        "research_output": research_output,
        "verified": verified,
        "verdict": verdict,
        "confidence": confidence,
        # FR16: Proov's reusable proof-of-verification artifact, attached to the companion's
        # own delivery (the tx-bearing form when the order anchored, else the in-band badge, else
        # the honest error signal).
        "verified_by_proov": verified_artifact,
        "proov_order": {"order_id": proov_order_id, "explorer_url": explorer_url},
    }

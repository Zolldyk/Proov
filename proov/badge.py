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

import html
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

    Hardened (Story 4.2): this FR16 reuse seam is now invoked **directly** by the Epic-4
    companion caller (`proov.companion.extract_verified_artifact`), so a partial/missing
    `receipt` or a non-dict `anchor`/`anchor_ref` must yield a sensible artifact (missing fields
    → `None`) instead of raising `KeyError`/`TypeError` — resolving the 1.6-review deferred-work
    item. `.get` defaults + `isinstance` guards do this WITHOUT changing the bytes of a *complete*
    receipt's artifact: every field below resolves identically for the full eight-key shape
    `build_receipt` emits, so `report_hash`/`receipt_id` (hashed — Story 1.4) never shift.
    """
    r = receipt if isinstance(receipt, dict) else {}
    anchor = anchor if isinstance(anchor, dict) else None
    anchor_ref = r.get("anchor_ref")
    return {
        "issuer": ISSUER,
        "schema": BADGE_SCHEMA,
        "version": r.get("version"),
        "verdict": r.get("verdict"),
        "confidence": r.get("confidence"),
        "model": r.get("model"),
        "timestamp": r.get("timestamp"),
        "output_hash": r.get("output_hash"),
        "report_hash": r.get("report_hash"),
        # Copy — never alias the receipt's nested dict (mutating one must not touch the other);
        # tolerate a missing/non-dict `anchor_ref` → `None` rather than crashing on `dict(...)`.
        "anchor_ref": dict(anchor_ref) if isinstance(anchor_ref, dict) else None,
        # Copy — never alias the caller's anchor dict (consistency with anchor_ref above; a
        # caller that reuses/mutates its anchor must not mutate this finalised artifact).
        "anchor": dict(anchor) if anchor else None,
        # The on-chain content_hash once it is concretely known, else the pre-delivery-stable
        # report_hash. Guard on the *value* (not just dict-presence) so an anchor that lacks
        # content_hash (or carries None) falls back to report_hash rather than yielding a
        # null receipt_id.
        "receipt_id": (anchor.get("content_hash") if anchor else None) or r.get("report_hash"),
        "verify": {"rule": _VERIFY_RULE, "procedure": _VERIFY_PROCEDURE},
    }


# --------------------------------------------------------------------------------------------
# Badge RENDERER (Story 4.3) — turn the artifact dict into a VISIBLE, embeddable badge.
#
# The payload above is a JSON dict; these render it into the two forms a caller actually embeds:
# an HTML snippet and a Markdown snippet. Pure, stdlib-only (`html.escape`), no `croo`/network/I/O
# — same purity contract as the builder. Self-contained (inline styles, no shields.io / external
# image host) so it stays `$0`/offline (NFR1) and the test suite never hits a socket.
#
# Two honesty invariants — the whole point of a verifier's badge (AC2/AC3, both non-negotiable):
#   * Verdict: the affirmative "✓ Verified by Proov" form renders ONLY for a genuine badge whose
#     verdict is exactly `"pass"` — the SAME gate `compose_delivery` uses (companion.py:152-156).
#     Any other / missing verdict renders a neutral, clearly-not-affirmative form showing the real
#     verdict ("Proov: partial" / "Proov: fail" / "Proov: unverified").
#   * Anchor: the BaseScan tx link + on-chain `content_hash` receipt id render ONLY for the
#     tx-bearing (anchored) form. The in-band / off-protocol PREVIEW form (`anchor` is null) says
#     "preview — not anchored on-chain" and shows NO tx link — the renderer never fabricates proof.

_AFFIRMATIVE_LABEL = "✓ Verified by Proov"


def _badge_view(artifact: object) -> dict:
    """Normalise an artifact into the honest facts the renderers display.

    Tolerant (AC8): a non-dict / partial / `{"error": …}` / `None` / schema-mismatch artifact is
    NOT a genuine badge and degrades to an honest "unverified" view — never a raise. The
    affirmative gate mirrors `compose_delivery` exactly: a genuine badge AND `verdict == "pass"`.
    """
    a = artifact if isinstance(artifact, dict) else {}
    is_badge = a.get("schema") == BADGE_SCHEMA
    verdict = a.get("verdict") if is_badge else None
    affirmative = is_badge and verdict == "pass"

    anchor = a.get("anchor") if is_badge else None
    anchored = isinstance(anchor, dict)
    explorer_url = anchor.get("explorer_url") if anchored else None
    # Only an `https://` URL is ever rendered as a clickable on-chain proof link. `html.escape`
    # neutralises quote/markup breakout but NOT a `javascript:`/`data:` scheme, and an arbitrary
    # non-explorer URL would be a fabricated "proof" — both forbidden by AC3/AC4. An
    # untrusted/missing URL degrades to the linkless "anchored on-chain" form. `explorer_tx_url`
    # only ever emits `https://basescan.org/tx/…`, so a genuine anchor always passes.
    if not (isinstance(explorer_url, str) and explorer_url.startswith("https://")):
        explorer_url = None

    if affirmative:
        status_text = _AFFIRMATIVE_LABEL
    elif is_badge:
        status_text = f"Proov: {verdict or 'unverified'}"
    else:
        status_text = "Proov: unverified"

    return {
        "is_badge": is_badge,
        "affirmative": affirmative,
        "verdict": verdict,
        "status_text": status_text,
        "anchored": anchored,
        "explorer_url": explorer_url,
        "receipt_id": a.get("receipt_id") if is_badge else None,
        "confidence": a.get("confidence") if is_badge else None,
        "model": a.get("model") if is_badge else None,
    }


def render_badge_html(artifact: dict) -> str:
    """Render a self-contained, embeddable HTML "Verified by Proov" badge (AC1).

    Pure `str` builder, `html.escape` on EVERY interpolated value (AC4 — the renderer is reused on
    the attacker-controlled `Try this` surface). Honest per AC2 (affirmative only on a passing
    genuine badge) and AC3 (tx link only on the anchored form). Degrades on a partial/None/non-
    badge artifact (AC8). Inline styles only — no external image host (NFR1).
    """
    v = _badge_view(artifact)
    accent = "#1b7f3b" if v["affirmative"] else "#9a6700"
    background = "#e7f5ec" if v["affirmative"] else "#fff4e0"
    status = html.escape(v["status_text"])

    parts = [
        '<span class="proov-badge" style="display:inline-block;font-family:system-ui,sans-serif;'
        f"font-size:.85rem;line-height:1.45;border:1px solid {accent};border-radius:6px;"
        f'background:{background};color:#1a1a1a;padding:.4rem .6rem">'
        f'<strong style="color:{accent}">{status}</strong>'
    ]

    if v["is_badge"]:
        details = []
        if v["verdict"] is not None:
            details.append(f"verdict: {html.escape(str(v['verdict']))}")
        if v["confidence"] is not None:
            details.append(f"confidence: {html.escape(str(v['confidence']))}")
        if v["model"]:
            details.append(f"model: {html.escape(str(v['model']))}")
        if details:
            parts.append(f'<br><small>{" &middot; ".join(details)}</small>')

        receipt_id = html.escape(str(v["receipt_id"])) if v["receipt_id"] else ""
        if v["anchored"] and v["explorer_url"]:
            url = html.escape(v["explorer_url"])
            proof = f'on-chain proof: <a href="{url}">{url}</a>'
        elif v["anchored"]:
            proof = "anchored on-chain"  # anchor present but no tx hash → no fabricated link
        else:
            proof = "preview &mdash; not anchored on-chain"
        if receipt_id:
            proof += f" &middot; receipt_id: <code>{receipt_id}</code>"
        parts.append(f"<br><small>{proof}</small>")

    parts.append("</span>")
    return "".join(parts)


def _md_inline(value: object) -> str:
    """Neutralise markup/link-breaking characters in a value placed inside Markdown (AC4).

    Collapses newlines and backslash-escapes the characters that could break out of `[text](url)`
    link syntax or inject markup, so a hostile verdict/model/hash can't escape the badge.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    for ch in ("\\", "`", "*", "_", "[", "]", "(", ")", "<", ">"):
        text = text.replace(ch, "\\" + ch)
    return text


def render_badge_markdown(artifact: dict) -> str:
    """Render a self-contained, embeddable Markdown "Verified by Proov" badge (AC1).

    Same honesty invariants as `render_badge_html` (AC2/AC3) and the same tolerant degrade (AC8).
    Every interpolated value passes through `_md_inline` so a hostile value can't break out of the
    badge's markdown; the BaseScan URL uses the angle-bracket link destination form. Pure, no I/O.
    """
    v = _badge_view(artifact)
    lines = [f"**{_md_inline(v['status_text'])}**"]

    if v["is_badge"]:
        details = []
        if v["verdict"] is not None:
            details.append(f"verdict: {_md_inline(v['verdict'])}")
        if v["confidence"] is not None:
            details.append(f"confidence: {_md_inline(v['confidence'])}")
        if v["model"]:
            details.append(f"model: {_md_inline(v['model'])}")
        if details:
            lines.append(" · ".join(details))

        receipt_id = _md_inline(v["receipt_id"]) if v["receipt_id"] else ""
        if v["anchored"] and v["explorer_url"]:
            # Angle-bracket destination tolerates any char but `>`/newline (both stripped below).
            url = v["explorer_url"].replace("\n", "").replace("\r", "").replace(">", "%3E")
            proof = f"on-chain proof: [{url}](<{url}>)"
        elif v["anchored"]:
            proof = "anchored on-chain"
        else:
            proof = "preview — not anchored on-chain"
        if receipt_id:
            proof += f" · receipt_id: {receipt_id}"
        lines.append(proof)

    # Two trailing spaces = a hard line break inside one Markdown block (the badge stays cohesive).
    return "  \n".join(lines)


# Convenience for callers that want canonical bytes of the artifact without reaching for
# `proov.receipt` (no hashing happens here; this only re-exports the serialiser).
def _canonical_json(obj: Any) -> str:  # pragma: no cover - thin re-export
    from .receipt import canonical_json

    return canonical_json(obj)

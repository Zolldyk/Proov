"""Testable core of the human "Try this" page (Story 4.1).

The first user-facing surface Proov ships. It runs the **identical** verification pipeline a
paid CAP order runs in `proov/provider.py` — `validate_requirements` → `engine.verify` →
`build_deliverable` — but **off-protocol**: no payment, no WebSocket, no `deliver_order`, no
on-chain anchor (FR19's free preview; the paid on-chain order + rendered badge is Story 4.3).
It re-implements NONE of the verdict/extraction/judgment/deliverable logic — it only
orchestrates the existing entrypoints, mirroring `provider._handle_order_paid`'s verify→build
block (`proov/provider.py:431-497`) and its degrade-don't-drop discipline.

SDK-agnostic by design (architecture §2: only `[A] provider.py` is CROO-coupled): NO `croo`
import. It imports only `proov.engine` / `proov.validation` / `proov.deliverable`. All logic
worth testing lives here as pure functions; `scripts/try_this.py` is a thin stdlib socket
shell over `run_demo_verification` / `render_form` / `render_result` (scripts are not unit
tested directly — same split as `dashboard.py`/`calibrate.py`).

Security: the output being verified is attacker-controlled, so `render_result`/`render_form`
HTML-escape EVERY dynamic value (`html.escape`) — this surface must not become reflected XSS.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import Any

from proov import deliverable as _deliverable
from proov import engine as _engine
from proov.validation import validate_requirements

log = logging.getLogger("proov.webdemo")

# The two tier literals (`proov/types.py:27`). Anything else → the permissive `"quick"`
# default, matching `services.tier_for_service`.
_VALID_TIERS = ("quick", "deep")


def _resolve_tier(tier: object) -> str:
    """Coerce a form-supplied tier to `"quick"`/`"deep"`; anything else → `"quick"`."""
    if isinstance(tier, str) and tier.strip().lower() in _VALID_TIERS:
        return tier.strip().lower()
    return "quick"


def _parse_sources(sources_text: str | None) -> list[dict]:
    """Parse the optional `sources` textarea (one URL per line) into PRD §6 `[{"url": …}]`.

    Blank/whitespace lines are dropped; a blank/empty field yields `[]` (caller omits the
    `sources` key entirely so `validate_requirements` never sees an empty list).
    """
    if not isinstance(sources_text, str) or not sources_text:
        return []
    sources = []
    for line in sources_text.splitlines():
        url = line.strip()
        if url:
            sources.append({"url": url})
    return sources


def run_demo_verification(
    output_text: str, sources_text: str | None = None, tier: object = "quick"
) -> dict:
    """Off-protocol twin of `provider._handle_order_paid`'s verify→build block.

    Builds the PRD §6 input dict from the form fields, validates it (a reject returns a
    structured `{"error_code", "reason"}` dict — never raises), then runs the SAME pipeline a
    paid order runs and maps the `Report` through `build_deliverable(order=None, …)`. The
    verify+build is wrapped in the provider's exact degrade discipline: on ANY `Exception`
    (a broken `verify` or a fault in `build_deliverable` itself) it falls back to
    `build_graceful_deliverable(order=None, …, reason="internal_verification_error")` — an
    honest `unverifiable` deliverable, degraded rather than dropped (NFR3).

    Returns either the full PRD §6 deliverable dict (success/degrade) or an error dict
    `{"error_code", "reason"}` for invalid input. Pass `tier` permissively; only `output_text`
    is required.
    """
    # Build the input dict the validator/engine expect; omit `sources` when empty so the
    # validator's optional-`sources` rule never trips on an empty list. `output_text` is passed
    # through unchanged so `validate_requirements` stays the single arbiter of the contract — a
    # non-string `output` becomes `output_not_string` rather than being silently coerced.
    input_obj: dict[str, Any] = {"output": output_text}
    sources = _parse_sources(sources_text)
    if sources:
        input_obj["sources"] = sources

    # Same defensive entry the provider uses: validate first, branch on `.ok`, never raise.
    result = validate_requirements(json.dumps(input_obj))
    if not result.ok:
        return {"error_code": result.code, "reason": result.reason}

    resolved_tier = _resolve_tier(tier)
    # `output_text` feeds the receipt's `output_hash` (Story 1.4) — use the VALIDATED value.
    validated_output = result.value["output"]

    # Degrade-don't-drop (AC5), mirroring provider.py:476-497. `engine.verify` itself never
    # raises out (it degrades internally to an honest `partial`), so the except is mostly
    # belt-and-suspenders — but a programming error in `build_deliverable` must still degrade
    # to an honest `unverifiable` rather than surface a 500/stack trace. Both resolved via the
    # module so a monkeypatched engine/builder is honoured (and the degrade is testable).
    try:
        report = asyncio.run(_engine.verify(result.value, resolved_tier))
        return _deliverable.build_deliverable(
            None, resolved_tier, output_text=validated_output, report=report
        )
    except Exception:
        # Degrade-don't-drop (NFR3), but log the swallowed cause — collapsing every fault into a
        # silent graceful deliverable otherwise makes a broken demo impossible to debug.
        log.exception("verify/build failed; degrading to honest unverifiable")
        return _deliverable.build_graceful_deliverable(
            None,
            resolved_tier,
            output_text=validated_output,
            reason="internal_verification_error",
        )


# --------------------------------------------------------------------------------------------
# Renderers — pure `str` builders. EVERY dynamic value is `html.escape`d (reflected-XSS guard).
# --------------------------------------------------------------------------------------------

_FREE_PREVIEW_NOTICE = (
    "This is a <strong>free, off-protocol preview</strong>: no CAP order is placed, no payment "
    "is made, and the receipt below is computed but <strong>NOT anchored on-chain</strong> "
    "(<code>verified_by_proov.anchor</code> is <code>null</code>). It runs the exact same "
    "verification pipeline a paid order runs. The paid on-chain order and rendered "
    "&ldquo;Verified by Proov&rdquo; badge are a later story."
)

_KEYLESS_NOTICE = (
    "With no API keys set this runs $0 offline (a stub LLM + Wikipedia) and is "
    "<em>optimistic</em> &mdash; set <code>GEMINI_API_KEY</code> for a meaningful demo."
)

_PAGE_CSS = (
    "body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;"
    "line-height:1.5;color:#1a1a1a}"
    "textarea{width:100%;box-sizing:border-box;font-family:ui-monospace,monospace}"
    ".notice{background:#fff8e1;border:1px solid #f0d264;border-radius:6px;padding:.75rem 1rem;"
    "margin:1rem 0}"
    ".verdict{font-size:1.6rem;font-weight:700;margin:.25rem 0}"
    ".pass{color:#1b7f3b}.fail{color:#b3261e}.partial,.unverifiable{color:#9a6700}"
    ".claim{border:1px solid #ddd;border-radius:6px;padding:.6rem .8rem;margin:.6rem 0}"
    ".quote{color:#444;border-left:3px solid #ccc;padding-left:.6rem;margin:.3rem 0}"
    "pre{background:#f6f6f6;border:1px solid #e0e0e0;border-radius:6px;padding:.8rem;"
    "overflow:auto;font-size:.85rem}"
    "label{font-weight:600}"
)


def _page(title: str, body: str) -> str:
    """Wrap a body fragment in a minimal, self-contained HTML document (no build, no JS)."""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_PAGE_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_form() -> str:
    """The GET `/` page: textarea + optional sources + tier radio + submit + honest banner."""
    body = (
        "<h1>Proov &mdash; Try this</h1>"
        f"<div class=\"notice\">{_FREE_PREVIEW_NOTICE}</div>"
        f"<div class=\"notice\">{_KEYLESS_NOTICE}</div>"
        "<form method=\"post\" action=\"/\">"
        "<p><label for=\"output\">AI output to verify</label><br>"
        "<textarea id=\"output\" name=\"output\" rows=\"8\" "
        "placeholder=\"Paste the AI-generated text you want checked…\" required></textarea></p>"
        "<p><label for=\"sources\">Sources (optional, one URL per line)</label><br>"
        "<textarea id=\"sources\" name=\"sources\" rows=\"3\" "
        "placeholder=\"https://en.wikipedia.org/wiki/…\"></textarea></p>"
        "<p><label>Tier:</label> "
        "<label><input type=\"radio\" name=\"tier\" value=\"quick\" checked> Quick</label> "
        "<label><input type=\"radio\" name=\"tier\" value=\"deep\"> Deep</label></p>"
        "<p><button type=\"submit\">Verify</button></p>"
        "</form>"
    )
    return _page("Proov — Try this", body)


def _render_error(error: dict) -> str:
    """Render a structured `validate_requirements` rejection as a clean `code: reason` block."""
    code = html.escape(str(error.get("error_code", "error")))
    reason = html.escape(str(error.get("reason", "")))
    body = (
        "<h1>Proov &mdash; Try this</h1>"
        "<div class=\"notice\"><strong>Could not verify this input.</strong></div>"
        f"<p><strong>{code}</strong></p><p>{reason}</p>"
        "<p><a href=\"/\">&larr; Back</a></p>"
    )
    return _page("Proov — input rejected", body)


def _render_claim(claim: dict) -> str:
    """Render one per-claim finding (claim, status, confidence, escaped evidence quotes)."""
    cid = html.escape(str(claim.get("id", "")))
    text = html.escape(str(claim.get("claim", "")))
    status = html.escape(str(claim.get("status", "")))
    confidence = html.escape(str(claim.get("confidence", "")))
    parts = [
        f"<div class=\"claim\"><div><strong>{status}</strong> "
        f"(confidence {confidence}) &mdash; <code>{cid}</code></div>"
        f"<div>{text}</div>"
    ]
    for ev in claim.get("evidence", []) or []:
        if not isinstance(ev, dict):
            continue
        source = html.escape(str(ev.get("source", "")))
        quote = html.escape(str(ev.get("quote", "")))
        stance = html.escape(str(ev.get("stance", "")))
        parts.append(f"<div class=\"quote\">[{stance}] {quote}<br><small>{source}</small></div>")
    parts.append("</div>")
    return "".join(parts)


def render_result(deliverable_or_error: dict) -> str:
    """Render the human verdict view, or a structured error page for rejected input.

    For a deliverable: a big verdict + confidence, the per-claim evidence trail, the citation
    flags, the disclaimer, the receipt `report_hash` / badge `receipt_id`, AND a `<pre>` dump
    of the full deliverable JSON so "verdict + evidence" is literally visible (FR17). EVERY
    dynamic value is `html.escape`d — the verified text is attacker-controlled.
    """
    if "error_code" in deliverable_or_error:
        return _render_error(deliverable_or_error)

    d = deliverable_or_error
    verdict = str(d.get("verdict", "unverifiable"))
    verdict_class = html.escape(verdict)
    confidence = html.escape(str(d.get("confidence", "")))
    summary = html.escape(str(d.get("summary", "")))

    parts = [
        "<h1>Proov &mdash; verification result</h1>",
        f"<div class=\"notice\">{_FREE_PREVIEW_NOTICE}</div>",
        f"<div class=\"verdict {verdict_class}\">{html.escape(verdict.upper())}</div>",
        f"<p>Confidence: <strong>{confidence}</strong></p>",
        f"<p>{summary}</p>",
    ]

    claims = d.get("claims") or []
    parts.append(f"<h2>Claims ({len(claims)})</h2>")
    for claim in claims:
        if isinstance(claim, dict):
            parts.append(_render_claim(claim))

    citations = d.get("citations_checked") or []
    if citations:
        parts.append(f"<h2>Citations checked ({len(citations)})</h2><ul>")
        for c in citations:
            if not isinstance(c, dict):
                continue
            source = html.escape(str(c.get("source", "")))
            flag = html.escape(str(c.get("flag", "")))
            retrievable = html.escape(str(c.get("retrievable", "")))
            supports = html.escape(str(c.get("supports_attached_claim", "")))
            parts.append(
                f"<li><code>{source}</code> &mdash; flag: {flag}, "
                f"retrievable: {retrievable}, supports: {supports}</li>"
            )
        parts.append("</ul>")

    receipt = d.get("receipt") or {}
    badge = d.get("verified_by_proov") or {}
    report_hash = html.escape(str(receipt.get("report_hash", "")))
    receipt_id = html.escape(str(badge.get("receipt_id", "")))
    anchor = badge.get("anchor")
    parts.append(
        "<h2>Receipt</h2>"
        f"<p>report_hash: <code>{report_hash}</code><br>"
        f"receipt_id: <code>{receipt_id}</code><br>"
        f"on-chain anchor: <code>{html.escape(str(anchor))}</code> "
        "(off-protocol preview &mdash; not anchored)</p>"
    )

    parts.append(f"<p><em>{html.escape(str(d.get('disclaimer', '')))}</em></p>")

    # The full deliverable JSON, so a human literally sees "verdict + evidence" (FR17).
    full_json = html.escape(json.dumps(d, indent=2, sort_keys=True))
    parts.append(f"<h2>Full deliverable</h2><pre>{full_json}</pre>")
    parts.append("<p><a href=\"/\">&larr; Verify another</a></p>")

    return _page("Proov — verification result", "".join(parts))

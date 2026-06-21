"""Pure input validator for the submitted negotiation `requirements` (Story 1.5).

SDK-agnostic and side-effect free (no `croo` import, no network, no logging) — mirrors the
style of `proov.services` / `proov.receipt`. The provider calls this BEFORE accepting a
negotiation (reject malformed → buyer never pays) and again defensively at the paid stage
(reject → escrow auto-refund).

Design stance (PRD §1 "a verifier that cries wolf is worse than useless"): **reject only on
clearly-malformed input.** A wrongly-rejected legitimate order costs a real external buyer —
the scarce thing this hackathon is graded on — so the hard rules below are the *only* reject
reasons; unknown keys, an odd `mode`, and extra `options` are tolerated (forward-compatible).
The tier is authoritative from `service_id` (see `proov.services`), never from `mode`.

`validate_requirements` returns a structured `ValidationResult` and **never raises** for
malformed input — callers branch on `.ok` and pass `.reason` straight to a CROO reject.
"""

from __future__ import annotations

import json
import os
from typing import NamedTuple

# --- Stable structured error codes (PRD §6 snake_case; safe as CROO reject reasons) ---
INVALID_JSON = "invalid_json"
MISSING_OUTPUT_FIELD = "missing_output_field"
EMPTY_OUTPUT_FIELD = "empty_output_field"
OUTPUT_NOT_STRING = "output_not_string"
OUTPUT_TOO_LARGE = "output_too_large"
INVALID_SOURCES = "invalid_sources"

# Generous default cap for "AI text to verify" (256 KB) — well under memory risk yet far
# above any legitimate claim/output payload. Checked on the RAW string BEFORE json.loads so
# an oversized payload never materialises a huge object (closes the 1.4 deferred MemoryError
# gap). Env-overridable for ops; falls back to the default on a missing/garbage value.
_DEFAULT_MAX_BYTES = 256 * 1024


def _default_max_bytes() -> int:
    raw = os.environ.get("PROOV_MAX_INPUT_BYTES")
    if raw is None:
        return _DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BYTES
    return value if value > 0 else _DEFAULT_MAX_BYTES


class ValidationResult(NamedTuple):
    """Outcome of validating a `requirements` string.

    `ok` True  → `value` is the normalised input dict; `code`/`reason` are None.
    `ok` False → `value` is None; `code` is a stable machine code (above) and `reason` is a
    human-readable `"code: detail"` string suitable as a CROO reject reason.
    """

    ok: bool
    value: dict | None = None
    code: str | None = None
    reason: str | None = None


def _reject(code: str, detail: str) -> ValidationResult:
    return ValidationResult(ok=False, value=None, code=code, reason=f"{code}: {detail}")


def _sources_ok(sources: object) -> bool:
    """`sources`, if present, must be a list of `{url}` objects with a non-empty string url."""
    if not isinstance(sources, list):
        return False
    for item in sources:
        if not isinstance(item, dict):
            return False
        url = item.get("url")
        if not isinstance(url, str) or url.strip() == "":
            return False
    return True


def validate_requirements(raw: str, *, max_bytes: int | None = None) -> ValidationResult:
    """Validate a negotiation's `requirements` JSON string against the PRD §6 input contract.

    Order of checks (size cap FIRST, before any parse):
      1. byte cap on the raw string        → OUTPUT_TOO_LARGE
      2. parseable AND a JSON object        → INVALID_JSON
      3. has an `output` key                → MISSING_OUTPUT_FIELD
      4. `output` is a string               → OUTPUT_NOT_STRING
      5. `output` is non-blank              → EMPTY_OUTPUT_FIELD
      6. `sources` (if present) is [{url}]  → INVALID_SOURCES

    On success returns the parsed dict as `value` (unknown keys preserved — forward
    compatible). Never raises for malformed input.
    """
    cap = _default_max_bytes() if max_bytes is None else max_bytes

    # 1. Size cap on the RAW bytes, before json.loads materialises anything.
    if not isinstance(raw, (str, bytes, bytearray)):
        return _reject(INVALID_JSON, "requirements is not a string")
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > cap:
        return _reject(
            OUTPUT_TOO_LARGE,
            f"requirements is {len(encoded)} bytes, over the {cap}-byte cap",
        )

    # 2. Parse + require a JSON object.
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return _reject(INVALID_JSON, "requirements is not parseable JSON")
    if not isinstance(obj, dict):
        return _reject(INVALID_JSON, "requirements is not a JSON object")

    # 3-5. The `output` field — required, a string, non-blank.
    if "output" not in obj:
        return _reject(MISSING_OUTPUT_FIELD, "no 'output' key in requirements")
    output = obj["output"]
    if not isinstance(output, str):
        return _reject(OUTPUT_NOT_STRING, "'output' must be a string")
    if output.strip() == "":
        return _reject(EMPTY_OUTPUT_FIELD, "'output' is blank/whitespace")

    # 6. Optional `sources` — structurally a list of {url} objects.
    if "sources" in obj and not _sources_ok(obj["sources"]):
        return _reject(
            INVALID_SOURCES, "'sources' must be a list of objects each with a non-empty 'url'"
        )

    # Success: tolerate everything else (mode/options/unknown keys are advisory). The tier
    # comes from service_id, not `mode`, so we do not reject or rewrite those here.
    return ValidationResult(ok=True, value=obj, code=None, reason=None)

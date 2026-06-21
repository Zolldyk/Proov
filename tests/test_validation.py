"""Tests for proov.validation — the pure, SDK-agnostic input validator (Story 1.5).

One assertion per validator branch. The validator NEVER raises for malformed input —
it returns a structured `ValidationResult` so callers branch on `.ok`. Stable machine
codes (PRD §6 snake_case) are asserted directly so a downstream CROO reject reason and
the README error table can rely on them.
"""

from __future__ import annotations

import json

from proov.validation import (
    EMPTY_OUTPUT_FIELD,
    INVALID_JSON,
    INVALID_SOURCES,
    MISSING_OUTPUT_FIELD,
    OUTPUT_NOT_STRING,
    OUTPUT_TOO_LARGE,
    ValidationResult,
    validate_requirements,
)

_GOOD_OUTPUT = "Paris is the capital of France."


def _req(**obj) -> str:
    return json.dumps(obj)


def test_valid_minimal_input_accepts_and_normalises():
    result = validate_requirements(_req(output=_GOOD_OUTPUT))
    assert isinstance(result, ValidationResult)
    assert result.ok is True
    assert result.code is None and result.reason is None
    assert result.value is not None
    assert result.value["output"] == _GOOD_OUTPUT


def test_valid_with_sources_and_optional_fields():
    raw = _req(
        output=_GOOD_OUTPUT,
        claims=["Paris is in France"],
        sources=[{"url": "https://example.com", "title": "Geo"}],
        mode="deep",
        options={"language": "en"},
    )
    result = validate_requirements(raw)
    assert result.ok is True
    assert result.value["sources"][0]["url"] == "https://example.com"


def test_tolerates_unknown_keys_and_odd_mode():
    # Forward-compatible: unknown keys + advisory mode/options never cause a reject.
    raw = _req(output=_GOOD_OUTPUT, mode="banana", surprise={"x": 1}, options=[1, 2])
    result = validate_requirements(raw)
    assert result.ok is True


def test_invalid_json_not_parseable():
    result = validate_requirements("not-json{")
    assert result.ok is False
    assert result.code == INVALID_JSON
    assert INVALID_JSON in result.reason
    assert result.value is None


def test_invalid_json_when_not_an_object():
    # Parseable JSON but not a JSON object (array / bare string / number) → invalid_json.
    for raw in ("[]", '"x"', "5", "true", "null"):
        result = validate_requirements(raw)
        assert result.ok is False, raw
        assert result.code == INVALID_JSON, raw


def test_missing_output_field():
    result = validate_requirements(_req(claims=["a"]))
    assert result.ok is False
    assert result.code == MISSING_OUTPUT_FIELD


def test_output_not_string():
    result = validate_requirements(_req(output=5))
    assert result.ok is False
    assert result.code == OUTPUT_NOT_STRING


def test_empty_output_field_blank_and_whitespace():
    for blank in ("", "   ", "\n\t "):
        result = validate_requirements(_req(output=blank))
        assert result.ok is False, repr(blank)
        assert result.code == EMPTY_OUTPUT_FIELD, repr(blank)


def test_output_too_large_caught_before_parse():
    # A small max_bytes forces the cap to trip; assert it is caught BEFORE json.loads
    # by feeding bytes that are ALSO invalid JSON — a parse-first impl would report
    # invalid_json instead of output_too_large.
    raw = "x" * 100  # invalid JSON AND over the cap
    result = validate_requirements(raw, max_bytes=10)
    assert result.ok is False
    assert result.code == OUTPUT_TOO_LARGE


def test_output_too_large_uses_byte_length_not_char_length():
    # Multi-byte UTF-8: 4 chars but >4 bytes — the cap is on encoded bytes.
    raw = _req(output="éééé")
    over = len(raw.encode("utf-8")) - 1
    result = validate_requirements(raw, max_bytes=over)
    assert result.ok is False
    assert result.code == OUTPUT_TOO_LARGE


def test_invalid_sources_not_a_list():
    result = validate_requirements(_req(output=_GOOD_OUTPUT, sources="https://x.com"))
    assert result.ok is False
    assert result.code == INVALID_SOURCES


def test_invalid_sources_item_missing_url():
    result = validate_requirements(_req(output=_GOOD_OUTPUT, sources=[{"title": "no url"}]))
    assert result.ok is False
    assert result.code == INVALID_SOURCES


def test_invalid_sources_item_not_a_dict():
    result = validate_requirements(_req(output=_GOOD_OUTPUT, sources=["https://x.com"]))
    assert result.ok is False
    assert result.code == INVALID_SOURCES


def test_invalid_sources_url_blank():
    result = validate_requirements(_req(output=_GOOD_OUTPUT, sources=[{"url": "   "}]))
    assert result.ok is False
    assert result.code == INVALID_SOURCES


def test_reason_is_code_colon_detail_shape():
    # The reason is a human-readable "code: detail" suitable as a CROO reject reason.
    result = validate_requirements(_req(claims=["a"]))
    assert result.reason.startswith(MISSING_OUTPUT_FIELD + ":")


def test_does_not_raise_on_non_string_input():
    # Defensive: a None / non-str raw is structured-rejected, never an exception.
    result = validate_requirements(None)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.code == INVALID_JSON

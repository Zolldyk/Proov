"""Tests for the human "Try this" demo core (`proov/webdemo.py`, Story 4.1).

Fully offline ($0, NFR3): the autouse fixture forces the deterministic stub LLM + stub search
providers (same approach as `tests/test_engine.py`), and the suite-wide `conftest.py` disables
the cache/ledger. NO socket is ever bound — `scripts/try_this.py` is a thin runner, so only the
pure functions in `webdemo.py` are tested here (the established `proov/` core vs `scripts/`
runner split). `run_demo_verification` calls `asyncio.run` internally, so its tests are plain
sync functions (not `async`), avoiding a nested running loop.
"""

from __future__ import annotations

import pytest

from proov import deliverable as deliverable_mod
from proov.validation import (
    EMPTY_OUTPUT_FIELD,
    OUTPUT_NOT_STRING,
    OUTPUT_TOO_LARGE,
)
from proov.webdemo import (
    _parse_sources,
    render_form,
    render_result,
    run_demo_verification,
)

# Every PRD §6 top-level key a paid CAP order's deliverable carries (AC3).
_PRD6_KEYS = {
    "verdict",
    "confidence",
    "summary",
    "claims",
    "citations_checked",
    "stats",
    "disclaimer",
    "receipt",
    "verified_by_proov",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Force deterministic offline stub providers so no test touches the network ($0)."""
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "stub")
    monkeypatch.setenv("PROOV_SEARCH_PROVIDER", "stub")
    monkeypatch.delenv("PROOV_QUICK_SLA_SECONDS", raising=False)


# --- AC2/AC3: valid input runs the SAME path and returns the full deliverable contract -------


def test_valid_input_returns_full_prd6_deliverable_contract():
    result = run_demo_verification("Paris is the capital of France.", "", "quick")
    assert isinstance(result, dict)
    assert "error_code" not in result
    # Same PRD §6 shape a paid CAP order yields (AC3) — produced by build_deliverable.
    assert _PRD6_KEYS.issubset(result.keys())
    assert result["verdict"] in {"pass", "fail", "partial", "unverifiable"}
    # AC4: off-protocol preview — the receipt exists but is NOT anchored on-chain.
    assert result["verified_by_proov"]["anchor"] is None


def test_tier_is_resolved_permissively():
    # Anything not "quick"/"deep" → "quick" (mirrors services.tier_for_service).
    result = run_demo_verification("A factual sentence.", "", "nonsense-tier")
    assert result["stats"]["tier"] == "quick"
    deep = run_demo_verification("A factual sentence.", "", "DEEP")
    assert deep["stats"]["tier"] == "deep"


def test_sources_textarea_is_parsed_into_prd6_url_objects():
    assert _parse_sources("https://a.example\n\n  https://b.example  \n") == [
        {"url": "https://a.example"},
        {"url": "https://b.example"},
    ]
    assert _parse_sources("") == []
    assert _parse_sources(None) == []
    assert _parse_sources("   \n  \n") == []


# --- AC5: every invalid input → a structured validate_requirements code, never an exception ---


def test_empty_output_returns_structured_error():
    result = run_demo_verification("", "", "quick")
    # A clean structured error dict — no exception, no deliverable.
    assert set(result.keys()) == {"error_code", "reason"}
    assert result["error_code"] == EMPTY_OUTPUT_FIELD
    assert isinstance(result["reason"], str)


def test_whitespace_only_output_returns_empty_field_error():
    result = run_demo_verification("    \n  ", "", "quick")
    assert result["error_code"] == EMPTY_OUTPUT_FIELD


def test_non_string_output_returns_output_not_string_error():
    # The runner always sends a str, but webdemo lets the validator be the single arbiter, so a
    # non-string output surfaces the real code rather than a coerced empty value.
    result = run_demo_verification(123, "", "quick")  # type: ignore[arg-type]
    assert result["error_code"] == OUTPUT_NOT_STRING


def test_oversized_output_returns_too_large_error(monkeypatch):
    monkeypatch.setenv("PROOV_MAX_INPUT_BYTES", "1024")
    result = run_demo_verification("x" * 2048, "", "quick")
    assert result["error_code"] == OUTPUT_TOO_LARGE


# --- AC5: a defensive failure in build_deliverable degrades, it does not 500 -----------------


def test_build_failure_degrades_to_honest_unverifiable(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("build exploded")

    monkeypatch.setattr(deliverable_mod, "build_deliverable", _boom)
    result = run_demo_verification("A factual sentence to verify.", "", "quick")
    # Degrade, don't drop: the graceful build still runs and yields an honest unverifiable.
    assert "error_code" not in result
    assert result["verdict"] == "unverifiable"
    assert result["stats"]["degraded"] is True
    assert _PRD6_KEYS.issubset(result.keys())


# --- AC1/AC8: renderers return str with the expected anchors and escape attacker text ---------


def test_render_form_has_inputs_and_honest_banner():
    html_str = render_form()
    assert isinstance(html_str, str)
    assert 'name="output"' in html_str
    assert 'name="sources"' in html_str
    assert 'name="tier"' in html_str
    # AC4 free-preview framing + AC6 keyless honesty caveat.
    assert "off-protocol" in html_str
    assert "GEMINI_API_KEY" in html_str


def test_render_result_shows_verdict_and_free_preview_disclaimer():
    deliverable = run_demo_verification("Paris is the capital of France.", "", "quick")
    html_str = render_result(deliverable)
    assert isinstance(html_str, str)
    assert deliverable["verdict"].upper() in html_str
    assert "off-protocol" in html_str  # AC4 disclaimer on the result page too


def test_render_result_escapes_attacker_controlled_text():
    # The verified output (hence the extracted claim text) is attacker-controlled — it must be
    # HTML-escaped, never reflected as live markup (reflected-XSS guard, AC8).
    xss = '<script>alert("pwned")</script>'
    deliverable = {
        "verdict": "pass",
        "confidence": 0.9,
        "summary": "ok",
        "claims": [
            {
                "id": "c1",
                "claim": xss,
                "status": "supported",
                "confidence": 0.9,
                "evidence": [{"source": xss, "quote": xss, "stance": "supports"}],
            }
        ],
        "citations_checked": [],
        "stats": {"tier": "quick"},
        "disclaimer": "disc",
        "receipt": {"report_hash": "abc"},
        "verified_by_proov": {"receipt_id": "abc", "anchor": None},
    }
    html_str = render_result(deliverable)
    assert "<script>alert" not in html_str
    assert "&lt;script&gt;" in html_str


def test_render_result_renders_structured_error_cleanly():
    html_str = render_result({"error_code": "empty_output_field", "reason": "x: blank"})
    assert "empty_output_field" in html_str
    assert "&larr; Back" in html_str or "Back" in html_str

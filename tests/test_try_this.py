"""Tests for the hardened "Try this" runner (`scripts/try_this.py`, Story 4.4) — OFFLINE.

`scripts/` is not a package, so the module is loaded by file path. NO real socket is bound: the
`BaseHTTPRequestHandler` is instantiated via `__new__` with in-memory `rfile`/`wfile` and driven
directly (the handler-level style the story prescribes). Covers the AC8 surface only — the bounded
concurrency 503, the read timeout being configured, and the defense-in-depth headers. The pure
verification core is `proov/webdemo.py` (tested in `tests/test_webdemo.py`).
"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import threading
from http.client import HTTPMessage

import pytest

_TRY_THIS_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "try_this.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_try_this_under_test", _TRY_THIS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try_this = _load_module()


@pytest.fixture(autouse=True)
def _reset_slots():
    """Keep the class-level concurrency gate from leaking between tests."""
    try_this._TryThisHandler._slots = None
    yield
    try_this._TryThisHandler._slots = None


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Force deterministic offline providers so a POST that reaches verify stays $0."""
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "stub")
    monkeypatch.setenv("PROOV_SEARCH_PROVIDER", "stub")


class _FakeConn:
    """A stand-in socket: records `settimeout`, hands back in-memory streams for `makefile`."""

    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def makefile(self, mode: str, bufsize: int):  # noqa: ANN001
        return io.BytesIO()


def _make_handler(*, path: str, body: bytes = b"", command: str = "POST", headers=None):
    """Instantiate the handler without binding a socket and wire in-memory request streams."""
    handler = try_this._TryThisHandler.__new__(try_this._TryThisHandler)
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    msg = HTTPMessage()
    for key, value in (headers or {}).items():
        msg[key] = value
    handler.headers = msg
    handler.path = path
    handler.command = command
    handler.request_version = "HTTP/1.1"
    handler.client_address = ("127.0.0.1", 5555)
    handler.requestline = f"{command} {path} HTTP/1.1"
    return handler


def _response_text(handler) -> str:
    return handler.wfile.getvalue().decode("utf-8", "replace")


# --------------------------------------------------------------------------- concurrency cap (503)


def test_post_over_capacity_returns_503():
    # A cap of 1, already fully acquired → the next POST is shed with a clean 503 (no hang).
    try_this._TryThisHandler._slots = threading.BoundedSemaphore(1)
    assert try_this._TryThisHandler._slots.acquire(blocking=False) is True  # occupy the only slot

    handler = _make_handler(
        path="/verify", body=b"output=hi", headers={"Content-Length": "9"}
    )
    handler.do_POST()
    text = _response_text(handler)
    assert " 503 " in text.splitlines()[0]
    assert "busy" in text.lower()


def test_post_within_capacity_runs_and_releases_slot():
    # With a free slot the POST runs the offline verification and the slot is released afterwards.
    sem = threading.BoundedSemaphore(1)
    try_this._TryThisHandler._slots = sem
    handler = _make_handler(
        path="/verify",
        body=b"output=Paris+is+the+capital+of+France.",
        headers={"Content-Length": "38"},
    )
    handler.do_POST()
    text = _response_text(handler)
    assert " 200 " in text.splitlines()[0]
    # The slot was released (finally block) — we can acquire it again without blocking.
    assert sem.acquire(blocking=False) is True


# --------------------------------------------------------------------------- read timeout


def test_setup_sets_socket_read_timeout(monkeypatch):
    monkeypatch.setenv("PROOV_TRYTHIS_READ_TIMEOUT", "12.5")
    fake = _FakeConn()
    handler = try_this._TryThisHandler.__new__(try_this._TryThisHandler)
    handler.request = fake
    handler.client_address = ("127.0.0.1", 1)
    handler.server = None
    handler.setup()
    assert fake.timeout == 12.5


def test_read_timeout_default_and_garbage(monkeypatch):
    monkeypatch.delenv("PROOV_TRYTHIS_READ_TIMEOUT", raising=False)
    assert try_this._read_timeout() == try_this._DEFAULT_READ_TIMEOUT
    monkeypatch.setenv("PROOV_TRYTHIS_READ_TIMEOUT", "nonsense")
    assert try_this._read_timeout() == try_this._DEFAULT_READ_TIMEOUT
    monkeypatch.setenv("PROOV_TRYTHIS_READ_TIMEOUT", "inf")
    assert try_this._read_timeout() == try_this._DEFAULT_READ_TIMEOUT


# --------------------------------------------------------------------------- security headers


def test_send_html_carries_nosniff_and_csp():
    handler = _make_handler(path="/", command="GET")
    handler.do_GET()
    text = _response_text(handler)
    assert "X-Content-Type-Options: nosniff" in text
    assert "Content-Security-Policy:" in text
    assert "default-src 'none'" in text
    # the self-posting form must still be allowed by the policy
    assert "form-action 'self'" in text


def test_max_concurrency_default_and_garbage(monkeypatch):
    monkeypatch.delenv("PROOV_TRYTHIS_MAX_CONCURRENCY", raising=False)
    assert try_this._max_concurrency() == try_this._DEFAULT_MAX_CONCURRENCY
    monkeypatch.setenv("PROOV_TRYTHIS_MAX_CONCURRENCY", "0")
    assert try_this._max_concurrency() == try_this._DEFAULT_MAX_CONCURRENCY
    monkeypatch.setenv("PROOV_TRYTHIS_MAX_CONCURRENCY", "2")
    assert try_this._max_concurrency() == 2

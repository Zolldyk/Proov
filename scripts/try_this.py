"""Human "Try this" free off-protocol verification page — thin stdlib runner (Story 4.1).

The operational socket shell over `proov/webdemo.py`. ALL testable logic (the orchestrator and
the renderers) lives in `proov.webdemo`; this file is a thin `http.server` runner only — the
same pure-core / runner split as `scripts/dashboard.py` and `scripts/calibrate.py`. No new
runtime dependency: Python stdlib `ThreadingHTTPServer` + `BaseHTTPRequestHandler`, NOT
FastAPI/Flask/uvicorn (architecture §6 — boring tech, $0, minimal deps).

Process isolation: this runs as its OWN process, separate from `python -m proov` (the provider's
persistent WebSocket loop). `ThreadingHTTPServer` is one-thread-per-request, so each request does
its own blocking `asyncio.run(engine.verify(...))` on its handler thread (inside
`run_demo_verification`) — it never shares or blocks the provider's event loop. Run both
side-by-side on the always-on host.

Run from the repo root:
    python scripts/try_this.py        # serves the free off-protocol demo on 127.0.0.1:8080

Config (env): PROOV_TRYTHIS_HOST (default 127.0.0.1), PROOV_TRYTHIS_PORT (default 8080).
With no API keys it runs $0 offline (stub LLM + Wikipedia); set GEMINI_API_KEY for a real demo.
"""

from __future__ import annotations

import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# Allow `python scripts/try_this.py` from the repo root to import the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proov.webdemo import render_form, render_result, run_demo_verification  # noqa: E402

log = logging.getLogger("try_this")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
# Socket-read cap mirroring the validator's input cap (`PROOV_MAX_INPUT_BYTES`, 256 KB): bound
# the raw POST body read so a giant Content-Length cannot be buffered before the validator —
# which also caps it — ever sees the string. Add headroom for form-encoding overhead.
_DEFAULT_MAX_BYTES = 256 * 1024


def _env_int(name: str, default: int) -> int:
    """Read a positive int env var; fall back to `default` on missing/garbage/≤0."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _max_body_bytes() -> int:
    return _env_int("PROOV_MAX_INPUT_BYTES", _DEFAULT_MAX_BYTES)


def _is_loopback(host: str) -> bool:
    """True if `host` only accepts local connections (default `127.0.0.1`/`localhost`/`::1`)."""
    return host in ("127.0.0.1", "localhost", "::1", "") or host.startswith("127.")


class _TryThisHandler(BaseHTTPRequestHandler):
    """Serve the GET form and run a POST verification — every response is `text/html`."""

    server_version = "ProovTryThis/0.1"

    def _send_html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            if self.path == "/":
                self._send_html(200, render_form())
            else:
                self._send_html(404, "<h1>404</h1><p>Not found. Try <a href=\"/\">/</a>.</p>")
        except Exception:  # never leak a traceback / reset the connection — emit a clean page
            self._send_error_page()

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            if self.path not in ("/", "/verify"):
                self._send_html(404, "<h1>404</h1><p>Not found. Try <a href=\"/\">/</a>.</p>")
                return
            # Reject (not truncate) an oversized body: silently verifying the first `cap` bytes
            # would check partial input the user never meant to submit. Honestly say it's too big.
            cap = _max_body_bytes()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                length = 0
            if length > cap:
                self._send_html(
                    413,
                    f"<h1>413</h1><p>Input too large (limit {cap} bytes). "
                    "Trim the text and <a href=\"/\">try again</a>.</p>",
                )
                return
            length = max(0, length)
            raw = self.rfile.read(length) if length else b""
            form = parse_qs(raw.decode("utf-8", errors="replace"))
            output_text = form.get("output", [""])[0]
            sources_text = form.get("sources", [""])[0]
            tier = form.get("tier", ["quick"])[0]
            result = run_demo_verification(output_text, sources_text, tier)
            self._send_html(200, render_result(result))
        except Exception:  # run_demo_verification degrades internally; this guards render/IO faults
            self._send_error_page()

    def _send_error_page(self) -> None:
        """Last-resort clean error response so a handler fault never resets the connection."""
        log.exception("unhandled error serving %s %s", self.command, self.path)
        try:
            self._send_html(
                500,
                "<h1>500</h1><p>Something went wrong handling this request. "
                "<a href=\"/\">Back</a>.</p>",
            )
        except Exception:
            log.exception("failed to send the 500 error page")

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:  # noqa: N802
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        self.send_error(405, "Method Not Allowed")

    def log_message(self, fmt: str, *args) -> None:  # quiet the default stderr spam
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("PROOV_TRYTHIS_HOST", _DEFAULT_HOST)
    port = _env_int("PROOV_TRYTHIS_PORT", _DEFAULT_PORT)
    httpd = ThreadingHTTPServer((host, port), _TryThisHandler)
    print(f"Proov 'Try this' demo (free, off-protocol) serving at http://{host}:{port}")
    print("This is a free preview — no CAP order, no payment, no on-chain anchor.")
    if not os.environ.get("GEMINI_API_KEY"):
        print("No GEMINI_API_KEY set: running $0 offline (stub + Wikipedia, optimistic).")
    if not _is_loopback(host):
        print(
            f"WARNING: bound to non-loopback host {host!r} — this exposes an UNAUTHENTICATED "
            "demo. User-supplied `sources` URLs are fetched server-side (SSRF risk: cloud "
            "metadata / internal services), and configured API keys spend free-tier quota on "
            "every request. Prefer 127.0.0.1, or front it with auth/rate-limiting before "
            "exposing it publicly."
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

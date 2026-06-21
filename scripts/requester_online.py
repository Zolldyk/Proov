"""Bring the Requester (test buyer) agent online.

Opens an authenticated WebSocket with `CROO_REQUESTER_API_KEY`; the handshake flips the
Requester agent `draft → online` in the CROO dashboard (same mechanism as `python -m
proov` for Proov). Holds the connection open — logging any events it receives — until
Ctrl-C, then closes cleanly.

This is a smoke test, not a long-running service:
- "online" lasts ONLY while this process runs; stop it and the agent returns to draft.
- It needs NO funding — the WS handshake is gas-sponsored and costs nothing.
- One WS per API key: don't run two processes on the same key (the server closes the
  second with WS 1008). This key is distinct from Proov's, so it won't clash with
  `python -m proov`.

Story 1.3's `scripts/place_test_order.py` will reuse this same connection to actually
negotiate + pay an order.

Run from the repo root:
    python scripts/requester_online.py
Then watch the CROO dashboard: the Requester agent should show `online`. Ctrl-C to stop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

# Allow `python scripts/requester_online.py` from the repo root to import the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proov.config import AppConfig, ConfigError  # noqa: E402
from proov.redaction import install_secret_redaction, register_secret  # noqa: E402


def _resolve_level(name: str) -> int:
    candidate = getattr(logging, name, None)
    return candidate if isinstance(candidate, int) else logging.INFO


async def _run(cfg: AppConfig) -> int:
    from croo import AgentClient, Config

    log = logging.getLogger("requester")

    if not cfg.requester_api_key:
        log.error("CROO_REQUESTER_API_KEY is not set in .env — cannot connect the Requester")
        return 1

    client = AgentClient(
        Config(base_url=cfg.api_url, ws_url=cfg.ws_url),
        sdk_key=cfg.requester_api_key,
    )
    stream = None
    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
            pass

    try:
        # The handshake is what flips the Requester draft → online.
        stream = await client.connect_websocket()
        stream.on_any(
            lambda e: log.info(
                "event: type=%s order_id=%s negotiation_id=%s",
                getattr(e, "type", None),
                getattr(e, "order_id", None),
                getattr(e, "negotiation_id", None),
            )
        )
        log.info("requester online: connected and listening (Ctrl-C to stop)")

        # Hold the process open; surface the non-recoverable duplicate-key (1008) case.
        while not shutdown.is_set():
            err = stream.err()
            if err is not None:
                log.error("fatal: %s", err)
                return 1
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        return 0
    finally:
        if stream is not None:
            try:
                await asyncio.wait_for(stream.close(), 10.0)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                log.warning("error closing websocket: %s", exc)
        try:
            await asyncio.wait_for(client.close(), 10.0)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            log.warning("error closing client: %s", exc)


def main() -> int:
    level = _resolve_level(os.environ.get("LOG_LEVEL", "INFO").upper())
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("croo").setLevel(level)  # surface SDK heartbeat/reconnect logs
    install_secret_redaction()  # ...but scrub the key the SDK prints in the connect URL

    try:
        cfg = AppConfig.from_env()
    except ConfigError as exc:
        logging.getLogger("requester").error("configuration error: %s", exc)
        return 1

    register_secret(cfg.api_key)
    register_secret(cfg.requester_api_key)

    try:
        return asyncio.run(_run(cfg))
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C race
        logging.getLogger("requester").info("interrupted; shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())

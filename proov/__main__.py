"""`python -m proov` — run the provider: it goes online and listens for events.

Exit codes:
- 0  clean shutdown (SIGINT/SIGTERM after a normal session).
- 1  config error, fatal provider error (e.g. duplicate-key 1008), or unexpected crash.

Running this is what flips Proov `draft → online` in the CROO dashboard, which also
completes Story 1.1's AC1 (Store discoverability).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .config import AppConfig, ConfigError
from .provider import FatalProviderError, ProviderAdapter
from .redaction import install_secret_redaction, register_secret


def _resolve_level(name: str) -> int:
    """Map a LOG_LEVEL name to a numeric level, defaulting to INFO.

    `getattr(logging, name)` can resolve to non-level attributes (e.g. `FILTER`,
    `LOGGER` → classes); only accept actual integer levels so a bad value can't crash
    `basicConfig`/`setLevel` with a TypeError.
    """
    candidate = getattr(logging, name, None)
    return candidate if isinstance(candidate, int) else logging.INFO


def _configure_logging() -> None:
    level = _resolve_level(os.environ.get("LOG_LEVEL", "INFO").upper())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The SDK logs reconnect/heartbeat under the `croo` logger — surface those.
    logging.getLogger("croo").setLevel(level)
    # ...but the SDK also prints the connection URL (with the API key) — scrub it.
    install_secret_redaction()


def main() -> int:
    _configure_logging()
    log = logging.getLogger("proov")

    try:
        cfg = AppConfig.from_env()
    except ConfigError as exc:
        # Never prints the key value — config only reports the missing var name.
        log.error("configuration error: %s", exc)
        return 1

    # Backstop: scrub the resolved key verbatim from any log line, even if it doesn't
    # match the `croo_sk_` shape (defense-in-depth for NFR5).
    register_secret(cfg.api_key)
    register_secret(cfg.requester_api_key)

    adapter = ProviderAdapter(cfg)
    try:
        asyncio.run(adapter.run())
    except FatalProviderError:
        # Already logged with guidance by the watchdog.
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C race
        # If a fatal (e.g. 1008) was already in flight when Ctrl-C surfaced, preserve
        # the non-zero exit AC3 requires rather than masking it as a clean interrupt.
        if adapter.fatal_error is not None:
            return 1
        log.info("interrupted; shutting down")
        return 0
    except Exception:  # pragma: no cover - defensive
        log.exception("provider crashed")
        return 1

    log.info("provider shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())

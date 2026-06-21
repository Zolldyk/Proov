"""Requester smoke-test harness — drive Proov's accept→pay→deliver→settle happy path.

TEST TOOLING, not product. Connects a **Requester** (test buyer) WebSocket using
`CROO_REQUESTER_API_KEY`, negotiates an order for one of Proov's services, pays it when
the order is created, and prints the delivered schema when the order completes. This is
the manual end-to-end smoke test referenced by README ("Happy-path smoke test").

Requires a **funded** Requester wallet: `pay_order` settles in real USDC on Base mainnet
(no testnet) and does an on-chain balance pre-check — it raises `InsufficientBalanceError`
if the wallet `0x30e9…0296` is unfunded. Until that wallet holds USDC (Story 1.1 AC2 open
item), this script will fail at the pay step; the automated suite covers the logic with a
fake client instead.

Run from the repo root (Proov must already be online via `python -m proov`):
    python scripts/place_test_order.py [service_id]
Defaults to the Quick Check service. The provider (Proov) and this Requester use distinct
API keys, so running both does NOT trip the one-WS-per-key (1008) rule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

# Allow `python scripts/place_test_order.py` from the repo root to import the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proov.config import AppConfig, ConfigError  # noqa: E402
from proov.redaction import install_secret_redaction, register_secret  # noqa: E402
from proov.services import QUICK_SERVICE_ID  # noqa: E402

# A PRD §6-shaped requirements payload (input contract). `output` is the text to verify.
_SAMPLE_REQUIREMENTS = {
    "output": "The Eiffel Tower is located in Paris, France and was completed in 1889.",
    "mode": "quick",
}


def _resolve_level(name: str) -> int:
    candidate = getattr(logging, name, None)
    return candidate if isinstance(candidate, int) else logging.INFO


async def _run(cfg: AppConfig, service_id: str) -> int:
    from croo import AgentClient, Config, EventType, NegotiateOrderRequest

    log = logging.getLogger("requester")

    if not cfg.requester_api_key:
        log.error("CROO_REQUESTER_API_KEY is not set in .env — cannot place a test order")
        return 1

    client = AgentClient(
        Config(base_url=cfg.api_url, ws_url=cfg.ws_url),
        sdk_key=cfg.requester_api_key,
    )
    stream = None
    done = asyncio.Event()
    tasks: set[asyncio.Task] = set()

    def _spawn(coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _pay(order_id: str) -> None:
        try:
            log.info("order created (order_id=%s) — paying", order_id)
            result = await client.pay_order(order_id)
            log.info("order paid: order_id=%s tx_hash=%s", order_id, getattr(result, "tx_hash", None))
        except Exception as exc:  # InsufficientBalanceError until the wallet is funded
            log.error("pay_order failed for %s: %s", order_id, exc)
            done.set()

    async def _show_delivery(order_id: str) -> None:
        try:
            delivery = await client.get_delivery(order_id)
            log.info("order completed (order_id=%s). Delivered schema:", order_id)
            schema = getattr(delivery, "deliverable_schema", "") or ""
            # Pretty-print if it parses as JSON; otherwise print raw.
            try:
                print(json.dumps(json.loads(schema), indent=2))
            except (ValueError, TypeError):
                print(schema)
        except Exception as exc:
            log.error("get_delivery failed for %s: %s", order_id, exc)
        finally:
            done.set()

    def _on_event(event) -> None:
        etype = getattr(event, "type", None)
        order_id = getattr(event, "order_id", None)
        if etype == EventType.ORDER_CREATED and order_id:
            _spawn(_pay(order_id), name=f"req-pay-{order_id}")
        elif etype == EventType.ORDER_COMPLETED and order_id:
            _spawn(_show_delivery(order_id), name=f"req-delivery-{order_id}")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, done.set)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
            pass

    try:
        stream = await client.connect_websocket()
        stream.on_any(_on_event)
        log.info("requester online; negotiating order for service_id=%s", service_id)

        negotiation = await client.negotiate_order(
            NegotiateOrderRequest(
                service_id=service_id,
                requirements=json.dumps(_SAMPLE_REQUIREMENTS),
            )
        )
        log.info(
            "negotiation created: negotiation_id=%s status=%s — waiting for order_created/paid/completed",
            getattr(negotiation, "negotiation_id", None),
            getattr(negotiation, "status", None),
        )

        # Wait for the happy path to complete (or a fatal pay/delivery error), with a
        # generous ceiling so the script doesn't hang forever if nothing comes back.
        try:
            await asyncio.wait_for(done.wait(), timeout=float(os.environ.get("SMOKE_TIMEOUT", "600")))
        except asyncio.TimeoutError:
            log.error("timed out waiting for the order to complete")
            return 1
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
    logging.getLogger("croo").setLevel(level)
    install_secret_redaction()

    # Target service: CLI arg > env > Quick Check default.
    service_id = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PROOV_TEST_SERVICE_ID") or QUICK_SERVICE_ID
    )

    try:
        cfg = AppConfig.from_env()
    except ConfigError as exc:
        logging.getLogger("requester").error("configuration error: %s", exc)
        return 1

    register_secret(cfg.api_key)
    register_secret(cfg.requester_api_key)

    try:
        return asyncio.run(_run(cfg, service_id))
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C race
        logging.getLogger("requester").info("interrupted; shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())

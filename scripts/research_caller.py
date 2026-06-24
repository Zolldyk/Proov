"""Companion "Research" caller — a real on-chain A2A composition demo (Story 4.2).

DEMO TOOLING, not the product. A thin, *separate* "Research" agent that produces an output and
then **hires Proov** to verify it before delivering: it places a real paid Quick Check order
against Proov over CAP (negotiate → pay → await completion → `get_delivery`), reads the
delivered "Verified by Proov" artifact + the on-chain anchor, and attaches it to its OWN
composed delivery — making the agent-hires-agent relationship a **real on-chain order**, visible
via `list_orders` / on Base. This is the on-protocol counterpart to Story 4.1's free off-protocol
preview (`scripts/try_this.py`).

It is a near-verbatim clone of `scripts/place_test_order.py` (the proven buyer harness) with a
composition tail bolted on — all testable logic lives in `proov/companion.py`; this runner is a
thin socket/buyer shell (not unit-tested directly).

Identity (anti-self-trade, AC5): runs as a DISTINCT registered agent on its OWN key
(`CROO_COMPANION_API_KEY`) so the A2A order is between two distinct agents and the dashboard's
self-trade accounting is legible — set `PROOV_OWN_AGENT_IDS` to this agent's id so its orders are
attributed to self-trade and excluded from the external-buyer count. **Run it sparingly**: it is
a demo; external orders must dominate (Story 4.4). If a dedicated companion key is not set, it
falls back to `CROO_REQUESTER_API_KEY` with a warning (then `PROOV_OWN_AGENT_IDS` should hold the
requester's agent id instead).

Funding (AC6): the live `pay_order` settles real USDC on Base mainnet (no testnet) with an
on-chain balance pre-check — it raises `InsufficientBalanceError` until the companion wallet is
funded (Story 1.1 AC2 / Epic 4.4 organizer-credit item), exactly like `place_test_order.py`. The
automated suite (`tests/test_companion.py`) covers the composition logic offline; this live run
is a smoke test, not a unit test.

Run from the repo root (Proov must already be online via `python -m proov`):
    python scripts/research_caller.py [service_id]
Defaults to the Quick Check service. Proov, the test requester, and this companion use DISTINCT
API keys, so running them together does NOT trip the one-WS-per-key (1008) rule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

# Allow `python scripts/research_caller.py` from the repo root to import the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proov.companion import (  # noqa: E402
    build_proov_input,
    compose_delivery,
    extract_verified_artifact,
    make_research_output,
    render_companion_delivery_markdown,
)
from proov.config import AppConfig, ConfigError  # noqa: E402
from proov.redaction import install_secret_redaction, register_secret  # noqa: E402
from proov.services import QUICK_SERVICE_ID  # noqa: E402


def _resolve_level(name: str) -> int:
    candidate = getattr(logging, name, None)
    return candidate if isinstance(candidate, int) else logging.INFO


def _resolve_companion_key(cfg: AppConfig, log: logging.Logger) -> str | None:
    """Pick the companion identity key — dedicated key preferred, requester key as fallback.

    A dedicated `CROO_COMPANION_API_KEY` makes the A2A order a trade between two *distinct*
    registered agents and keeps the self-trade accounting legible. If it is unset we fall back to
    the existing test requester key (with a loud warning) so the demo still runs — but then
    `PROOV_OWN_AGENT_IDS` must hold the requester's agent id instead.
    """
    if cfg.companion_api_key:
        return cfg.companion_api_key
    if cfg.requester_api_key:
        log.warning(
            "CROO_COMPANION_API_KEY is not set — falling back to CROO_REQUESTER_API_KEY. The "
            "A2A order will be placed by the requester agent; set PROOV_OWN_AGENT_IDS to the "
            "requester's agent id so self-trade accounting stays honest."
        )
        return cfg.requester_api_key
    return None


async def _run(cfg: AppConfig, service_id: str) -> int:
    from croo import AgentClient, Config, EventType, NegotiateOrderRequest

    log = logging.getLogger("companion")

    companion_key = _resolve_companion_key(cfg, log)
    if not companion_key:
        log.error(
            "no companion key available — set CROO_COMPANION_API_KEY (preferred) or "
            "CROO_REQUESTER_API_KEY in .env to place the composition order"
        )
        return 1

    # The thin research output this companion will hire Proov to verify (built-in sample / a
    # caller-supplied topic via PROOV_RESEARCH_TOPIC). The point is the composition, not research.
    research_output = make_research_output(os.environ.get("PROOV_RESEARCH_TOPIC"))
    requirements = build_proov_input(research_output)

    client = AgentClient(
        Config(base_url=cfg.api_url, ws_url=cfg.ws_url),
        sdk_key=companion_key,
    )
    stream = None
    done = asyncio.Event()
    tasks: set[asyncio.Task] = set()
    # Dedup ledgers: settle/compose each order_id at most once. The WS can redeliver a lifecycle
    # event (auto-reconnect buffer replay, Story 3.3) — without these a replayed ORDER_CREATED
    # would call pay_order twice and settle real USDC twice; a replayed ORDER_COMPLETED would
    # re-fetch and re-print the delivery. These are also the only guard against acting on a
    # foreign order_id (negotiate_order returns a negotiation_id, not the order_id, so the order
    # cannot be correlated up front — but a single demo run only ever pays/composes one order).
    paid: set[str] = set()
    composed: set[str] = set()

    def _spawn(coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _pay(order_id: str) -> None:
        try:
            log.info("order created (order_id=%s) — paying", order_id)
            result = await client.pay_order(order_id)
            log.info("order paid: order_id=%s tx_hash=%s", order_id, getattr(result, "tx_hash", None))
        except Exception as exc:  # InsufficientBalanceError until the companion wallet is funded
            log.error("pay_order failed for %s: %s", order_id, exc)
            done.set()

    async def _compose_and_show(order_id: str) -> None:
        """ORDER_COMPLETED (pushed to the BUYER) → fetch Proov's delivery, attach its badge,
        and emit the companion's OWN composed delivery (the demo artifact)."""
        try:
            delivery = await client.get_delivery(order_id)
            schema = getattr(delivery, "deliverable_schema", "") or ""
            try:
                deliverable = json.loads(schema)
            except (ValueError, TypeError):
                log.error("could not parse Proov's deliverable_schema for %s", order_id)
                deliverable = {}

            # The on-chain anchor data the provider stamped at deliver (Story 1.4): read defensively
            # — a missing tx just means no explorer link (the in-band badge still applies).
            content_hash = getattr(delivery, "content_hash", None)
            deliver_tx_hash = getattr(delivery, "deliver_tx_hash", None) or getattr(
                delivery, "tx_hash", None
            )
            delivery_id = getattr(delivery, "delivery_id", None)

            # FR16 consumer seam — turn Proov's deliverable into the "Verified by Proov" artifact,
            # then compose the companion's own delivery carrying it (verify-before-deliver).
            artifact = extract_verified_artifact(
                deliverable,
                content_hash=content_hash,
                deliver_tx_hash=deliver_tx_hash,
                order_id=order_id,
                delivery_id=delivery_id,
            )
            composed = compose_delivery(
                research_output=research_output,
                verified_artifact=artifact,
                proov_order_id=order_id,
            )
            log.info(
                "composition complete (order_id=%s verified=%s) — companion delivery:",
                order_id,
                composed.get("verified"),
            )
            print(json.dumps(composed, indent=2))
            # Story 4.3: show the badge literally "rendering on a caller's delivery" (FR16 in use).
            # When the order anchored on-chain this is the tx-bearing badge (BaseScan link); offline
            # / pre-funding it is the honest in-band preview. Markdown to stdout is enough for the demo.
            print("\n" + render_companion_delivery_markdown(composed))
        except Exception as exc:
            log.error("compose/get_delivery failed for %s: %s", order_id, exc)
        finally:
            done.set()

    # Terminal-failure event names: the SDK has no single failure EventType referenced in this
    # codebase, so match by name (robust to the exact enum members the SDK ships). Any event whose
    # type name carries one of these is a dead end → stop waiting instead of hanging to SMOKE_TIMEOUT.
    _FAILURE_MARKERS = ("FAIL", "REJECT", "CANCEL", "ERROR", "EXPIRE", "DECLINE", "TIMEOUT")

    def _on_event(event) -> None:
        etype = getattr(event, "type", None)
        order_id = getattr(event, "order_id", None)
        if etype == EventType.ORDER_CREATED and order_id:
            if order_id in paid:  # replayed event → don't pay (real USDC) twice
                log.debug("duplicate ORDER_CREATED for %s — already paying/paid, ignoring", order_id)
                return
            paid.add(order_id)
            _spawn(_pay(order_id), name=f"companion-pay-{order_id}")
        elif etype == EventType.ORDER_COMPLETED and order_id:
            if order_id in composed:  # replayed completion → don't re-fetch/re-print
                log.debug("duplicate ORDER_COMPLETED for %s — already composed, ignoring", order_id)
                return
            composed.add(order_id)
            _spawn(_compose_and_show(order_id), name=f"companion-compose-{order_id}")
        else:
            # A terminal failure/rejection emits no ORDER_CREATED/COMPLETED — without this branch
            # the run would block until SMOKE_TIMEOUT. Stop early on any failure-named event.
            ename = (getattr(etype, "name", None) or str(etype) or "").upper()
            if any(marker in ename for marker in _FAILURE_MARKERS):
                log.error("terminal event %s (order_id=%s) — aborting", ename, order_id)
                done.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, done.set)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
            pass

    try:
        stream = await client.connect_websocket()
        stream.on_any(_on_event)
        log.info(
            "companion online; hiring Proov for service_id=%s (verifying its own research output)",
            service_id,
        )

        negotiation = await client.negotiate_order(
            NegotiateOrderRequest(
                service_id=service_id,
                requirements=json.dumps(requirements),
            )
        )
        log.info(
            "negotiation created: negotiation_id=%s status=%s — waiting for order_created/paid/completed",
            getattr(negotiation, "negotiation_id", None),
            getattr(negotiation, "status", None),
        )

        # Wait for the happy path (or a fatal pay/compose error), with a generous ceiling so the
        # script doesn't hang forever if nothing comes back.
        try:
            await asyncio.wait_for(done.wait(), timeout=float(os.environ.get("SMOKE_TIMEOUT", "600")))
        except asyncio.TimeoutError:
            log.error("timed out waiting for the composition order to complete")
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
        logging.getLogger("companion").error("configuration error: %s", exc)
        return 1

    # Redact every key value from logs (the companion key is a distinct secret).
    register_secret(cfg.api_key)
    register_secret(cfg.requester_api_key)
    register_secret(cfg.companion_api_key)

    try:
        return asyncio.run(_run(cfg, service_id))
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C race
        logging.getLogger("companion").info("interrupted; shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())

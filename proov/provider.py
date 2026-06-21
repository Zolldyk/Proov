"""`[A]` CAP Provider Adapter — the only CROO-coupled module.

Responsibilities:
- Build the `AgentClient` from `AppConfig` and open the persistent WebSocket
  (`connect_websocket()` IS the handshake that flips Proov `draft → online`). [1.2]
- Register event handlers so the provider is *listening* (proves liveness). [1.2]
- Keep the process alive, watch for the non-recoverable duplicate-key (`1008`) error,
  and shut down cleanly on signals. [1.2]
- Drive the order happy path [1.3]: on `order_negotiation_created`, accept the
  negotiation (plain `accept_negotiation` — services are Require-Fund-Transfer OFF); on
  `order_paid`, fetch the order, build a schema-valid stub deliverable, and
  `deliver_order` it (which settles → `completed`). Completion is read from the
  `deliver_order` return value — `order_completed` is pushed to the Requester, NOT here.

As of Story 1.4 the delivered payload carries a REAL on-chain receipt: the provider reads
the submitted input text from the negotiation (`get_negotiation(...).requirements.output`)
for the receipt's `output_hash`, and delivers CANONICAL JSON so the keccak256 anchor is
independently reproducible (see `proov.receipt` / the README "Verify a receipt" section).

As of Story 1.5 the adapter validates the submitted input (see `proov.validation`) and
fails gracefully: a malformed negotiation is `reject_negotiation`'d (buyer never pays); a
malformed PAID order is `reject_order`'d (CAP escrow auto-refunds); and an internal
verification/build error delivers an honest `unverifiable` report (degrade, don't drop —
NFR3) rather than dropping the order to an SLA timeout. SLA-timeout refunds themselves are
platform-automatic — the provider never manually refunds.

What this module does NOT do: reconnect/heartbeat (the SDK's `EventStream` owns those —
30s ping, 60s pong-timeout, exponential backoff), real verification (Epic 2 — the verdict
is still an explicit stub; only the receipt is real), per-order timeout enforcement (Epic 2
/ Story 3.3), or worker-pool concurrency throttling (Epic 3 / Story 3.3).

Handlers are SYNCHRONOUS `Callable[[Event], None]` invoked from the SDK read-loop's
dispatch — they must not block and must not be `async`. Real async work is offloaded via
`asyncio.create_task` from inside the sync handler, with task references held so Python
doesn't GC a pending task. Each negotiation is accepted at most once and each order
delivered at most once (idempotent per id).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from .config import AppConfig

logger = logging.getLogger("proov.provider")


class FatalProviderError(RuntimeError):
    """Non-recoverable provider error (e.g. duplicate-key 1008) that must exit non-zero."""


class ProviderAdapter:
    """Owns the provider connection lifecycle for a single CROO SDK key.

    One adapter == one WebSocket == one SDK key. The CROO server closes a *second*
    concurrent connection on the same key with WS code 1008 (policy violation); the
    SDK records that in `EventStream.err()` and stops reconnecting, and our watchdog
    surfaces it as a `FatalProviderError`.
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        client: Any | None = None,
        watchdog_interval: float = 5.0,
        close_timeout: float = 10.0,
    ) -> None:
        self._cfg = cfg
        # `client` is injectable for unit tests; built from config on start() otherwise.
        self._client = client
        self._watchdog_interval = watchdog_interval
        self._close_timeout = close_timeout
        self._stream: Any | None = None
        self._shutdown = asyncio.Event()
        self._fatal_error: BaseException | None = None
        self._signals_installed = False
        # Idempotency guards: a negotiation is accepted at most once and an order
        # delivered at most once. Ids are added on first dispatch (before the await) so
        # a duplicate event that arrives while the first is still in flight is skipped.
        self._accepted_negotiations: set[str] = set()
        self._delivered_orders: set[str] = set()
        # Hold references to dispatched tasks so Python can't GC a still-pending task
        # ("Task was destroyed but it is pending"); each task removes itself on done.
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def fatal_error(self) -> BaseException | None:
        """The captured non-recoverable error, if one tripped the watchdog (else None)."""
        return self._fatal_error

    # --- connection -------------------------------------------------------------

    def _build_client(self) -> Any:
        # Imported lazily so config/tests stay SDK-decoupled. Import name is `croo`.
        from croo import AgentClient, Config

        return AgentClient(
            Config(base_url=self._cfg.api_url, ws_url=self._cfg.ws_url),
            sdk_key=self._cfg.api_key,
        )

    async def start(self) -> None:
        """Open the WebSocket and register handlers. Completing this is the handshake."""
        from croo import EventType

        if self._client is None:
            self._client = self._build_client()

        # connect_websocket() is async, returns an EventStream, and raises ValueError
        # if ws_url is empty. The SDK's read-loop + ping-loop start as background tasks.
        self._stream = await self._client.connect_websocket()

        # on_any proves we're listening (logs every received event). The NEGOTIATION_
        # CREATED + ORDER_PAID handlers drive the order happy path (Story 1.3).
        self._stream.on_any(self._log_event)
        self._stream.on(EventType.NEGOTIATION_CREATED, self._on_negotiation_created)
        self._stream.on(EventType.ORDER_PAID, self._on_order_paid)

        logger.info("provider online: listening for events")

    # --- task dispatch ----------------------------------------------------------

    def _spawn(self, coro: Any, *, name: str) -> None:
        """Schedule `coro` as a tracked background task (hold a ref so it isn't GC'd)."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- handlers (SYNC, must not block) ---------------------------------------

    def _log_event(self, event: Any) -> None:
        logger.info(
            "event received: type=%s order_id=%s negotiation_id=%s",
            getattr(event, "type", None),
            getattr(event, "order_id", None),
            getattr(event, "negotiation_id", None),
        )

    def _on_negotiation_created(self, event: Any) -> None:
        negotiation_id = getattr(event, "negotiation_id", None)
        if not negotiation_id:
            logger.warning("negotiation_created event without a negotiation_id; ignoring")
            return
        self._spawn(
            self._accept_negotiation(negotiation_id),
            name=f"proov-accept-{negotiation_id}",
        )

    def _on_order_paid(self, event: Any) -> None:
        order_id = getattr(event, "order_id", None)
        if not order_id:
            logger.warning("order_paid event without an order_id; ignoring")
            return
        self._spawn(
            self._handle_order_paid(order_id),
            name=f"proov-deliver-{order_id}",
        )

    # --- order happy path (async, offloaded from the sync handlers) -------------

    async def _accept_negotiation(self, negotiation_id: str) -> None:
        """Validate a new negotiation, then accept-or-reject it exactly once (Story 1.5).

        The negotiation stage is the strongest "never charged for nothing": rejecting the
        negotiation (`reject_negotiation`) before `accept_negotiation` means NO on-chain
        `createOrder` happens, so the buyer never pays. Valid input is accepted exactly as
        before (plain accept — services are Require-Fund-Transfer OFF).

        The id is marked "handled" before awaiting (idempotent per id). A successful accept
        OR a successful reject is terminal; only a *transient* infra failure (the
        get/accept/reject call itself raised) rolls back so a re-emit retries. Errors are
        logged and swallowed so a failure never crashes the read loop.
        """
        from .validation import validate_requirements

        if negotiation_id in self._accepted_negotiations:
            logger.info("negotiation %s already handled/in-flight; skipping", negotiation_id)
            return
        # Mark before awaiting so a duplicate event arriving mid-flight is also skipped.
        self._accepted_negotiations.add(negotiation_id)
        try:
            neg = await self._client.get_negotiation(negotiation_id)
            result = validate_requirements(getattr(neg, "requirements", "") or "")
            if not result.ok:
                # Malformed input → reject the NEGOTIATION (buyer never pays). Terminal.
                await self._client.reject_negotiation(negotiation_id, result.reason)
                logger.info(
                    "rejected malformed negotiation: negotiation_id=%s code=%s",
                    negotiation_id,
                    result.code,
                )
                return
            # Plain accept ONLY: services are Require-Fund-Transfer OFF, so the backend
            # rejects the fund-address variant. No `needEvaluation` arg exists.
            accepted = await self._client.accept_negotiation(negotiation_id)
            order = getattr(accepted, "order", None)
            logger.info(
                "negotiation accepted: negotiation_id=%s order_id=%s status=%s",
                negotiation_id,
                getattr(order, "order_id", None),
                getattr(order, "status", None),
            )
        except Exception as exc:
            # Roll back the guard so a transient failure can be retried on a re-emit.
            self._accepted_negotiations.discard(negotiation_id)
            logger.exception("failed to handle negotiation %s: %s", negotiation_id, exc)

    async def _handle_order_paid(self, order_id: str) -> dict | None:
        """Fetch a paid order, build the stub deliverable, deliver it, and return the artifact.

        NOTE (observed live 2026-06-21): `deliver_order` returns with the order in
        `delivering`, NOT `completed` — CLEAR/settlement is **asynchronous** server-side
        and lands ~1 min later (verified: 10% platform fee, 90% to the provider AA wallet
        via the order's `clear_tx_hash`). `order_completed` is pushed to the *Requester*,
        not the Provider, so we do not block on it here; the deliver tx + `content_hash`
        (the on-chain anchor used in Story 1.4) are the provider's evidence. Idempotent
        per order id; errors are logged and swallowed (graceful-partial/refund is 1.5).

        On a delivered order this RETURNS the post-delivery, tx-bearing "Verified by Proov"
        artifact (Story 1.6 / FR16) — the reusable surface the Epic 4 caller attaches to its
        own delivery. The dispatched task discards the value, but tests + that caller can use
        it. Reject / degrade-drop / failure paths return `None`.
        """
        from croo import DeliverableType, DeliverOrderRequest

        from . import deliverable as deliverable_mod
        from .deliverable import build_graceful_deliverable
        from .receipt import canonical_json
        from .services import tier_for_service
        from .validation import validate_requirements

        if order_id in self._delivered_orders:
            logger.info("order %s already delivered/in-flight; skipping", order_id)
            return
        self._delivered_orders.add(order_id)
        # Once deliver_order OR reject_order has returned, the order is terminal (anchored
        # on-chain, or rejected → escrow auto-refunds); a later failure (e.g. logging) must
        # NOT roll back the guard, or a re-emitted order_paid would deliver/reject the SAME
        # order twice. Only pre-terminal failures roll back so a re-emit retries.
        terminal = False
        try:
            order = await self._client.get_order(order_id)
            tier = tier_for_service(getattr(order, "service_id", ""))
            # The submitted input lives on the Negotiation (Order has no `requirements`).
            raw_requirements = await self._fetch_requirements(order)
            result = validate_requirements(raw_requirements)
            if not result.ok:
                # Defence-in-depth: a paid order with malformed input → reject_order, which
                # auto-refunds the Requester. We do NOT deliver. Terminal (no re-reject).
                await self._client.reject_order(order_id, result.reason)
                terminal = True
                logger.info(
                    "rejected paid order (escrow refund): order_id=%s code=%s",
                    order_id,
                    result.code,
                )
                return
            # `output_text` feeds the receipt's `output_hash` (Story 1.4).
            output_text = result.value["output"]
            # Graceful-degrade seam (AC4): the verification/build step is the only thing
            # wrapped here. Today it builds the stub; the Epic 2 `verify()` plugs in here.
            # On an internal error we deliver an honest `unverifiable` report (degrade,
            # don't drop) rather than letting the order fall to an SLA timeout. Resolve the
            # builder via the module so a monkeypatched build is honoured.
            #
            # The graceful fallback build AND the canonical serialisation are wrapped
            # together: if the DEGRADE build itself fails (it shares the badge/receipt/
            # canonical code with the stub — Story 1.6), or the deliverable will not
            # serialise, that is a DETERMINISTIC fault every retry would hit identically.
            # We must NOT fall to the outer `except` (which rolls back the idempotency guard
            # → poison-message redelivery loop). The honest terminal action is reject_order
            # (escrow auto-refunds the requester), exactly like the malformed-input path.
            try:
                try:
                    payload = deliverable_mod.build_stub_deliverable(
                        order, tier, output_text=output_text
                    )
                except Exception as build_exc:
                    logger.exception(
                        "verification/build failed for order %s; delivering graceful "
                        "unverifiable report (degrade, don't drop): %s",
                        order_id,
                        build_exc,
                    )
                    payload = build_graceful_deliverable(
                        order,
                        tier,
                        output_text=output_text,
                        reason="internal_verification_error",
                    )
                # Deliver CANONICAL bytes: the CAP backend anchors keccak256 of the EXACT
                # POSTed string, and `get_delivery` returns a re-ordered copy — so a verifier
                # can only reproduce `content_hash` by re-canonicalising. Delivering canonical
                # JSON makes that round-trip exact. (Story 1.4 Task 1 empirical finding.)
                deliverable_schema = canonical_json(payload)
            except Exception as fatal_build_exc:
                # Could not build/serialise a deliverable even after the graceful degrade —
                # reject so escrow refunds, and mark terminal so a re-emit does not retry the
                # same deterministic failure forever (poison message).
                await self._client.reject_order(order_id, "internal_verification_error")
                terminal = True
                logger.exception(
                    "rejected paid order (escrow refund) — could not build/serialise a "
                    "deliverable even after graceful degrade: order_id=%s: %s",
                    order_id,
                    fatal_build_exc,
                )
                return
            req = DeliverOrderRequest(
                deliverable_type=DeliverableType.SCHEMA,
                deliverable_schema=deliverable_schema,
            )
            delivery_result = await self._client.deliver_order(order_id, req)
            terminal = True  # anchored on-chain — never roll back the guard now
            delivered = getattr(delivery_result, "order", None)
            delivery = getattr(delivery_result, "delivery", None)
            content_hash = getattr(delivery, "content_hash", None)
            logger.info(
                "order delivered: order_id=%s tier=%s status=%s tx_hash=%s "
                "delivery_id=%s content_hash=%s",
                order_id,
                tier,
                getattr(delivered, "status", None),  # live: "delivering" (CLEAR is async)
                getattr(delivery_result, "tx_hash", None),
                getattr(delivery, "delivery_id", None),
                content_hash,  # on-chain anchor (Story 1.4)
            )
            # Per-order evidence line so the on-chain receipt anchor is captured in logs.
            logger.info(
                "receipt anchored: order_id=%s content_hash=%s output_hash=%s",
                order_id,
                content_hash,
                payload["receipt"]["output_hash"],
            )
            # Story 1.6: assemble the post-delivery, tx-bearing "Verified by Proov" artifact
            # (FR16) from data already in hand — the receipt + the now-known on-chain anchor.
            # DEFENSIVE: its own try/except (log-and-swallow). The order is already terminal
            # (anchored on-chain); a formatting/field error here must NOT trip the outer
            # `except` — that would log a misleading "failed to deliver" — nor (though
            # `terminal` already guards it) roll back the idempotency guard. Returned so the
            # Epic 4 caller can attach it to its own delivery.
            try:
                from .badge import build_anchor, build_verified_artifact

                anchor = build_anchor(
                    order_id=order_id,
                    content_hash=content_hash,
                    deliver_tx_hash=getattr(delivery_result, "tx_hash", None)
                    or getattr(delivered, "deliver_tx_hash", None),
                    delivery_id=getattr(delivery, "delivery_id", None),
                )
                artifact = build_verified_artifact(payload["receipt"], anchor=anchor)
                logger.info(
                    "verified-by-proov artifact: order_id=%s %s",
                    order_id,
                    canonical_json(artifact),
                )
                return artifact
            except Exception as artifact_exc:  # pragma: no cover - defensive log-and-swallow
                logger.exception(
                    "failed to assemble verified-by-proov artifact for delivered order "
                    "%s (delivery already anchored — not a delivery failure): %s",
                    order_id,
                    artifact_exc,
                )
                return None
        except Exception as exc:
            # Roll back the guard ONLY if the order is not yet terminal, so a transient
            # failure can retry on a re-emit. Once deliver_order OR reject_order succeeded,
            # keep the guard set so a post-terminal error (logging, etc.) can't trigger a
            # double-deliver / double-reject.
            if not terminal:
                self._delivered_orders.discard(order_id)
            logger.exception("failed to deliver order %s: %s", order_id, exc)

    async def _fetch_requirements(self, order: Any) -> str:
        """Read the raw `requirements` JSON string from the order's negotiation.

        The submitted input lives on the Negotiation (Order has no `requirements`). Returns
        the raw string for `validate_requirements` to parse; an order with no negotiation_id
        yields `""`, which the validator structurally rejects (defence-in-depth → refund).
        Subsumes Story 1.4's `_fetch_output_text` so there is a single input parser.
        """
        negotiation_id = getattr(order, "negotiation_id", "") or ""
        if not negotiation_id:
            logger.warning(
                "order %s has no negotiation_id; treating input as empty",
                getattr(order, "order_id", None),
            )
            return ""
        neg = await self._client.get_negotiation(negotiation_id)
        return getattr(neg, "requirements", "") or ""

    # --- lifecycle --------------------------------------------------------------

    async def run(self) -> None:
        """Connect, then stay alive until shutdown; raise on a fatal error.

        Holds the process open on an `asyncio.Event` so the SDK's background WS tasks
        keep running. A watchdog polls `stream.err()` for the non-recoverable 1008 case.
        On a fatal error the captured exception is re-raised so the entrypoint exits
        non-zero; a clean signal-driven shutdown returns normally (exit zero).
        """
        # `_aclose()` runs in the outer finally so a failure during start()/connect
        # (after the client's HTTP session is built) still releases that session.
        try:
            await self.start()
            self._install_signal_handlers()

            watchdog = asyncio.create_task(self._watchdog(), name="proov-watchdog")
            try:
                await self._shutdown.wait()
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
        finally:
            await self._aclose()

        if self._fatal_error is not None:
            raise self._fatal_error

    async def _watchdog(self) -> None:
        """Poll `stream.err()`; on a non-None error, record it and trigger shutdown."""
        while not self._shutdown.is_set():
            try:
                err = self._stream.err() if self._stream is not None else None
            except Exception as exc:
                # err() itself failing is a fatal condition — surface it rather than
                # letting the watchdog task die silently (which would bypass cleanup).
                err = exc
            if err is not None:
                self._handle_fatal(err)
                return
            # Sleep until the interval elapses OR shutdown is requested, whichever first.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._watchdog_interval
                )

    def _handle_fatal(self, err: BaseException) -> None:
        message = str(err)
        lowered = message.lower()
        if "policy violation" in lowered or "duplicate sdk-key" in lowered:
            logger.error(
                "fatal: another provider is already connected with this key "
                "(duplicate SDK-key, WS 1008). Only ONE `python -m proov` may run per "
                "CROO_API_KEY. Stop the other instance, then restart. (%s)",
                message,
            )
        else:
            logger.error("fatal provider error: %s", message)
        self._fatal_error = FatalProviderError(message)
        self._shutdown.set()

    def request_shutdown(self) -> None:
        """Idempotently signal a graceful shutdown (used by signal handlers/tests)."""
        self._shutdown.set()

    async def _aclose(self) -> None:
        """Close the WebSocket (normal 1000) then release the client's HTTP session."""
        # Time-bound each close so a half-open socket can't wedge shutdown after the
        # signal already fired (otherwise the only recourse would be SIGKILL).
        if self._stream is not None:
            try:
                await asyncio.wait_for(self._stream.close(), self._close_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "timed out closing websocket after %ss", self._close_timeout
                )
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.warning("error closing websocket: %s", exc)
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.close(), self._close_timeout)
            except asyncio.TimeoutError:
                logger.warning("timed out closing client after %ss", self._close_timeout)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.warning("error closing client: %s", exc)

    def _install_signal_handlers(self) -> None:
        """Wire SIGINT/SIGTERM to a graceful shutdown.

        Prefers the event-loop-native handler; falls back to `signal.signal`. Tolerant
        of environments where neither is available (e.g. non-main-thread under pytest).
        """
        if self._signals_installed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no running loop
            return

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):
                # add_signal_handler unsupported (e.g. Windows / not main thread):
                # fall back to the plain handler, scheduling the set on the loop.
                try:
                    signal.signal(
                        sig,
                        lambda *_a: loop.call_soon_threadsafe(self.request_shutdown),
                    )
                except (ValueError, OSError):  # pragma: no cover - cannot install
                    pass
        self._signals_installed = True

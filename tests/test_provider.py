"""Tests for proov.provider.ProviderAdapter using a fake EventStream/client.

No real socket is opened and the SDK's reconnect/heartbeat internals (owned by
`croo.ws`) are NOT re-tested here — only our adapter's wiring and lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from croo import (
    AcceptNegotiationResult,
    DeliverableType,
    DeliverOrderResult,
    Delivery,
    EventType,
    Negotiation,
    Order,
)
from proov.config import AppConfig
from proov.provider import (
    FatalProviderError,
    ProviderAdapter,
    _BoundedIdSet,
    _resolve_idempotency_max,
    _resolve_max_concurrent_orders,
    _resolve_shutdown_drain_seconds,
    _resolve_upload_threshold,
)
from proov.receipt import canonical_json, keccak256_hex
from proov.services import DEEP_SERVICE_ID, QUICK_SERVICE_ID

_DUMMY_KEY = "croo_sk_dummy_test_key"
# Sample submitted input the test buyer would negotiate (Negotiation.requirements JSON).
_SAMPLE_OUTPUT = "Paris is the capital of France."
_SAMPLE_REQUIREMENTS = json.dumps({"output": _SAMPLE_OUTPUT, "mode": "quick"})


def _cfg() -> AppConfig:
    return AppConfig(
        api_url="https://api.test",
        ws_url="wss://api.test/ws",
        api_key=_DUMMY_KEY,
    )


@pytest.fixture(autouse=True)
def _offline_engine(monkeypatch):
    """Force the deterministic stub LLM + stub search so the wired engine runs $0/offline.

    As of Story 2.6 a paid order runs the real `engine.verify`; without this the engine
    would resolve the live Gemini provider (no key → degrade) and the keyless Wikipedia
    search fallback (a live HTTP call). Pinning both to the stubs keeps the suite offline
    (NFR1) and makes the happy path deliver a real, deterministic `pass` verdict.
    """
    monkeypatch.setenv("PROOV_LLM_PROVIDER", "stub")
    monkeypatch.setenv("PROOV_SEARCH_PROVIDER", "stub")


class FakeEventStream:
    """Records handler registrations; exposes a settable err() and async close()."""

    def __init__(self) -> None:
        self.on_calls: list[tuple[str, object]] = []
        self.on_any_calls: list[object] = []
        self._err: Exception | None = None
        self.closed = False

    def on(self, event_type, handler):
        self.on_calls.append((event_type, handler))

    def on_any(self, handler):
        self.on_any_calls.append(handler)

    def dispatch(self, event):
        """Mimic the SDK read-loop: invoke the type-matched handler(s) + on_any, SYNC."""
        etype = getattr(event, "type", None)
        for evt_type, handler in self.on_calls:
            if evt_type == etype:
                handler(event)
        for handler in self.on_any_calls:
            handler(event)

    def err(self):
        return self._err

    def set_err(self, exc):
        self._err = exc

    async def close(self):
        self.closed = True


class FakeClient:
    """Stand-in for AgentClient: hands back a fake stream and records call activity.

    Returns real `croo` dataclasses for fidelity. Per-method `*_error` attributes let a
    test force a failure to exercise the log-and-swallow path.
    """

    def __init__(self, stream: FakeEventStream, *, service_id: str = QUICK_SERVICE_ID) -> None:
        self._stream = stream
        self.closed = False
        self._service_id = service_id
        self.accept_calls: list[str] = []
        self.get_order_calls: list[str] = []
        self.get_negotiation_calls: list[str] = []
        self.deliver_calls: list[tuple[str, object]] = []
        self.reject_negotiation_calls: list[tuple[str, str]] = []
        self.reject_order_calls: list[tuple[str, str]] = []
        self.accept_error: Exception | None = None
        self.get_order_error: Exception | None = None
        self.get_negotiation_error: Exception | None = None
        self.deliver_error: Exception | None = None
        self.reject_negotiation_error: Exception | None = None
        self.reject_order_error: Exception | None = None
        # Story 2.7 object-storage methods (big-report upload).
        self.upload_calls: list[tuple[str, bytes]] = []
        self.download_url_calls: list[str] = []
        self.upload_error: Exception | None = None
        # Override per-test to exercise the malformed-requirements path.
        self.requirements: str = _SAMPLE_REQUIREMENTS
        # Story 3.2 metrics-ledger fields the returned Order carries (empty by default → the
        # Order's own defaults, so existing tests are unaffected; set per-test to assert the
        # recorded counterparty/wallet/price).
        self.order_requester_agent_id: str = ""
        self.order_requester_wallet_address: str = ""
        self.order_price: str = ""

    async def connect_websocket(self):
        return self._stream

    async def close(self):
        self.closed = True

    async def accept_negotiation(self, negotiation_id: str):
        self.accept_calls.append(negotiation_id)
        if self.accept_error is not None:
            raise self.accept_error
        order = Order(order_id="ord-from-neg", negotiation_id=negotiation_id, status="created")
        return AcceptNegotiationResult(
            negotiation=Negotiation(negotiation_id=negotiation_id, status="accepted"),
            order=order,
        )

    async def get_order(self, order_id: str):
        self.get_order_calls.append(order_id)
        if self.get_order_error is not None:
            raise self.get_order_error
        return Order(
            order_id=order_id,
            service_id=self._service_id,
            negotiation_id=f"neg-of-{order_id}",
            status="paid",
            requester_agent_id=self.order_requester_agent_id,
            requester_wallet_address=self.order_requester_wallet_address,
            price=self.order_price,
        )

    async def reject_negotiation(self, negotiation_id: str, reason: str):
        self.reject_negotiation_calls.append((negotiation_id, reason))
        if self.reject_negotiation_error is not None:
            raise self.reject_negotiation_error

    async def get_negotiation(self, negotiation_id: str):
        self.get_negotiation_calls.append(negotiation_id)
        if self.get_negotiation_error is not None:
            raise self.get_negotiation_error
        return Negotiation(negotiation_id=negotiation_id, requirements=self.requirements)

    async def reject_order(self, order_id: str, reason: str):
        self.reject_order_calls.append((order_id, reason))
        if self.reject_order_error is not None:
            raise self.reject_order_error

    async def upload_file(self, file_name: str, body):
        self.upload_calls.append((file_name, bytes(body)))
        if self.upload_error is not None:
            raise self.upload_error
        return f"obj/{file_name}"

    async def get_download_url(self, object_key: str):
        self.download_url_calls.append(object_key)
        return f"https://dl.test/{object_key}"

    async def deliver_order(self, order_id: str, req):
        self.deliver_calls.append((order_id, req))
        if self.deliver_error is not None:
            raise self.deliver_error
        return DeliverOrderResult(
            # Live: deliver_order returns "delivering" — CLEAR/settlement is async
            # server-side and "completed"/clear_tx_hash land ~1 min later (Finding A).
            order=Order(order_id=order_id, status="delivering"),
            delivery=Delivery(delivery_id="dlv-1", order_id=order_id, content_hash="0xanchor"),
            tx_hash="0xdeadbeef",
        )


class FakeLedger:
    """In-memory `OrderLedger` stand-in (Story 3.2) — records, or raises on demand.

    Injected via `monkeypatch.setattr(proov.ledger, "get_order_ledger", lambda: fake)`; the
    provider's `_record_order` lazily imports the factory so the patch is honoured. With
    `raise_on_record=True` it simulates a broken ledger to prove the record hook is best-effort
    (a delivered order still delivers).
    """

    def __init__(self, *, raise_on_record: bool = False) -> None:
        self.records: list = []
        self._raise = raise_on_record

    async def record(self, record) -> None:
        if self._raise:
            raise RuntimeError("ledger boom")
        self.records.append(record)

    async def all_orders(self) -> list:
        return list(self.records)


def _event(**kwargs):
    """Build a minimal Event-like object exposing .type/.order_id/.negotiation_id."""
    return SimpleNamespace(
        type=kwargs.get("type"),
        order_id=kwargs.get("order_id"),
        negotiation_id=kwargs.get("negotiation_id"),
    )


async def _drain(adapter: ProviderAdapter) -> None:
    """Yield the loop until the adapter's dispatched create_task coroutines finish."""
    for _ in range(10):
        await asyncio.sleep(0)
        if not adapter._tasks:
            return


async def test_start_registers_listener_and_order_paid_handler():
    stream = FakeEventStream()
    adapter = ProviderAdapter(_cfg(), client=FakeClient(stream))

    await adapter.start()

    # on_any logger registered exactly once (proves we're listening).
    assert len(stream.on_any_calls) == 1
    # Both the NEGOTIATION_CREATED and ORDER_PAID handlers are registered.
    registered = {evt for evt, _ in stream.on_calls}
    assert EventType.NEGOTIATION_CREATED in registered
    assert EventType.ORDER_PAID in registered


async def test_negotiation_created_accepts_once_and_is_idempotent():
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    evt = _event(type=EventType.NEGOTIATION_CREATED, negotiation_id="neg-1")
    stream.dispatch(evt)
    await _drain(adapter)
    # A duplicate event must NOT trigger a second accept.
    stream.dispatch(evt)
    await _drain(adapter)

    assert client.accept_calls == ["neg-1"]


async def test_order_paid_fetches_and_delivers_schema():
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-1"))
    await _drain(adapter)

    assert client.get_order_calls == ["ord-1"]
    assert len(client.deliver_calls) == 1
    delivered_order_id, req = client.deliver_calls[0]
    assert delivered_order_id == "ord-1"
    # Delivered as a SCHEMA with a JSON-parseable payload conforming to PRD §6.
    assert req.deliverable_type == DeliverableType.SCHEMA
    payload = json.loads(req.deliverable_schema)
    # Story 2.6: the real engine now delivers a real verdict. The sample output
    # ("Paris is the capital of France.") extracts one claim the stub judges `supported`
    # → `pass` (no longer the stub `unverifiable`).
    assert payload["verdict"] == "pass"
    assert "summary" in payload and "receipt" in payload
    assert payload["stats"].get("degraded") is None  # a real run, not a degrade


async def test_order_paid_fetches_negotiation_and_populates_receipt():
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-r"))
    await _drain(adapter)

    # The submitted input is read from the negotiation (Order has no `requirements`).
    assert client.get_negotiation_calls == ["neg-of-ord-r"]
    _, req = client.deliver_calls[0]
    receipt = json.loads(req.deliverable_schema)["receipt"]
    # Receipt is populated (not the Story 1.3 `{}`), and output_hash == keccak256(output).
    assert receipt != {}
    assert receipt["output_hash"] == keccak256_hex(_SAMPLE_OUTPUT.encode("utf-8"))
    # Story 2.6: the receipt now stamps the REAL model id (the stub engine's honest id),
    # never the old `stub-no-engine` placeholder.
    assert receipt["model"] == "stub-llm"


async def test_delivered_payload_lets_a_verifier_reproduce_report_hash():
    # The core Story 1.4 contract: a verifier strips `receipt` from the DELIVERED canonical
    # JSON, re-canonicalises, keccak256s, and reproduces `report_hash` — from the delivered
    # bytes alone (no producer-side state). This guards against a top-level key being added
    # outside the hashed report body, which would silently break reproducibility.
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-v"))
    await _drain(adapter)

    _, req = client.deliver_calls[0]
    delivered = json.loads(req.deliverable_schema)
    receipt = delivered["receipt"]
    # Story 1.6: strip BOTH the `receipt` and the new `verified_by_proov` sibling.
    body = {k: v for k, v in delivered.items() if k not in ("receipt", "verified_by_proov")}
    reproduced = keccak256_hex(canonical_json(body).encode("utf-8"))
    assert reproduced == receipt["report_hash"]


async def test_delivered_schema_carries_in_band_verified_by_proov_badge():
    # The in-band artifact is inside the delivered (hashed) bytes, anchor null.
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-inband"))
    await _drain(adapter)

    _, req = client.deliver_calls[0]
    delivered = json.loads(req.deliverable_schema)
    badge = delivered["verified_by_proov"]
    assert badge["anchor"] is None  # tx/content_hash not known pre-delivery
    assert badge["receipt_id"] == delivered["receipt"]["report_hash"]
    assert badge["schema"] == "proov.verified-by-proov.v1"


# ---------------------------------------------- Deep big-report upload (Story 2.7)


def test_resolve_upload_threshold_hardening():
    # default 51200; garbage / non-int / ≤0 → default; a valid value is honoured.
    assert _resolve_upload_threshold(None) == 51200
    assert _resolve_upload_threshold("1024") == 1024
    assert _resolve_upload_threshold("nonsense") == 51200
    assert _resolve_upload_threshold("0") == 51200
    assert _resolve_upload_threshold("-5") == 51200


async def test_deep_large_report_uploaded_and_linked(monkeypatch):
    # With a low threshold a Deep deliverable is uploaded and linked via `report_file`; the
    # sibling does NOT change the embedded report_hash (strip receipt+badge+report_file).
    monkeypatch.setenv("PROOV_DEEP_UPLOAD_THRESHOLD_BYTES", "10")
    stream = FakeEventStream()
    client = FakeClient(stream, service_id=DEEP_SERVICE_ID)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-deep"))
    await _drain(adapter)

    assert len(client.deliver_calls) == 1
    assert len(client.upload_calls) == 1
    file_name, uploaded_bytes = client.upload_calls[0]
    assert file_name == "proov-report-ord-deep.json"
    assert client.download_url_calls == ["obj/proov-report-ord-deep.json"]

    _, req = client.deliver_calls[0]
    delivered = json.loads(req.deliverable_schema)
    rf = delivered["report_file"]
    assert rf["object_key"] == "obj/proov-report-ord-deep.json"
    assert rf["download_url"] == f"https://dl.test/{rf['object_key']}"
    assert rf["size_bytes"] == len(uploaded_bytes)

    # `report_file` is a post-receipt sibling: stripping receipt + verified_by_proov +
    # report_file reproduces report_hash from the delivered bytes.
    receipt = delivered["receipt"]
    body = {
        k: v
        for k, v in delivered.items()
        if k not in ("receipt", "verified_by_proov", "report_file")
    }
    assert keccak256_hex(canonical_json(body).encode("utf-8")) == receipt["report_hash"]

    # The UPLOADED bytes are the canonical deliverable WITHOUT report_file → the downloaded
    # file (minus receipt+badge) also reproduces report_hash.
    uploaded = json.loads(uploaded_bytes.decode("utf-8"))
    assert "report_file" not in uploaded
    ubody = {k: v for k, v in uploaded.items() if k not in ("receipt", "verified_by_proov")}
    assert keccak256_hex(canonical_json(ubody).encode("utf-8")) == receipt["report_hash"]


async def test_deep_upload_failure_degrades_to_inline_delivery(monkeypatch, caplog):
    # An upload that RAISES must not drop the order: it delivers inline WITHOUT report_file.
    monkeypatch.setenv("PROOV_DEEP_UPLOAD_THRESHOLD_BYTES", "10")
    stream = FakeEventStream()
    client = FakeClient(stream, service_id=DEEP_SERVICE_ID)
    client.upload_error = RuntimeError("upload flaked")
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    with caplog.at_level(logging.WARNING):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-deep-fail"))
        await _drain(adapter)

    assert len(client.deliver_calls) == 1  # still delivered (never dropped)
    _, req = client.deliver_calls[0]
    delivered = json.loads(req.deliverable_schema)
    assert "report_file" not in delivered
    assert client.reject_order_calls == []  # best-effort upload, not a fatal build error


async def test_quick_order_never_uploads(monkeypatch):
    # The tier gate: even with a tiny threshold, a Quick order never calls upload.
    monkeypatch.setenv("PROOV_DEEP_UPLOAD_THRESHOLD_BYTES", "1")
    stream = FakeEventStream()
    client = FakeClient(stream, service_id=QUICK_SERVICE_ID)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-quick"))
    await _drain(adapter)

    assert len(client.deliver_calls) == 1
    assert client.upload_calls == []
    delivered = json.loads(client.deliver_calls[0][1].deliverable_schema)
    assert "report_file" not in delivered


async def test_deep_sub_threshold_does_not_upload():
    # A small Deep deliverable below the (default 50KB) threshold delivers inline, no upload.
    stream = FakeEventStream()
    client = FakeClient(stream, service_id=DEEP_SERVICE_ID)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-deep-small"))
    await _drain(adapter)

    assert len(client.deliver_calls) == 1
    assert client.upload_calls == []
    delivered = json.loads(client.deliver_calls[0][1].deliverable_schema)
    assert "report_file" not in delivered


async def test_handle_order_paid_returns_tx_bearing_artifact():
    # Story 1.6 AC4: a delivered order returns the post-delivery artifact carrying the
    # now-known on-chain anchor (content_hash / deliver_tx_hash), receipt_id == content_hash.
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    # Await directly so we can read the return value the dispatched task would discard.
    artifact = await adapter._handle_order_paid("ord-art")

    assert artifact is not None
    anchor = artifact["anchor"]
    # The FakeClient deliver_order returns content_hash="0xanchor", tx_hash="0xdeadbeef".
    assert anchor["content_hash"] == "0xanchor"
    assert anchor["deliver_tx_hash"] == "0xdeadbeef"
    assert anchor["delivery_id"] == "dlv-1"
    assert anchor["explorer_url"] == "https://basescan.org/tx/0xdeadbeef"
    assert artifact["receipt_id"] == anchor["content_hash"]
    assert artifact["schema"] == "proov.verified-by-proov.v1"


async def test_handle_order_paid_returns_none_on_reject():
    # A rejected (refunded) order is never delivered → no tx-bearing artifact.
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.requirements = "not-json{"
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    artifact = await adapter._handle_order_paid("ord-rej")
    assert artifact is None
    assert len(client.reject_order_calls) == 1


async def test_order_paid_with_malformed_requirements_rejects_for_refund(caplog):
    # Story 1.5 REPLACES the 1.4 "default empty output, still deliver" behaviour: a paid
    # order with malformed input is now reject_order'd (escrow auto-refunds) — never
    # delivered as an empty-output stub.
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.requirements = "not-json{"
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    with caplog.at_level(logging.INFO, logger="proov.provider"):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-bad"))
        await _drain(adapter)

    assert len(client.reject_order_calls) == 1
    rejected_id, reason = client.reject_order_calls[0]
    assert rejected_id == "ord-bad"
    assert "invalid_json" in reason  # structured reason carried to CROO
    assert client.deliver_calls == []  # never delivered


async def test_negotiation_with_invalid_requirements_is_rejected_not_accepted():
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.requirements = "not-json{"
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.NEGOTIATION_CREATED, negotiation_id="neg-bad"))
    await _drain(adapter)

    # Negotiation stage: malformed input → reject_negotiation (buyer NEVER pays), no accept.
    assert client.reject_negotiation_calls == [("neg-bad", client.reject_negotiation_calls[0][1])]
    assert "invalid_json" in client.reject_negotiation_calls[0][1]
    assert client.accept_calls == []


async def test_negotiation_with_valid_requirements_is_accepted_not_rejected():
    stream = FakeEventStream()
    client = FakeClient(stream)  # default requirements are valid
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.NEGOTIATION_CREATED, negotiation_id="neg-ok"))
    await _drain(adapter)

    assert client.accept_calls == ["neg-ok"]
    assert client.reject_negotiation_calls == []


async def test_order_paid_valid_input_delivers_as_before():
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-good"))
    await _drain(adapter)

    assert len(client.deliver_calls) == 1
    assert client.reject_order_calls == []
    _, req = client.deliver_calls[0]
    payload = json.loads(req.deliverable_schema)
    # Valid input still feeds the real output_hash (not the degraded path).
    assert payload["receipt"]["output_hash"] == keccak256_hex(_SAMPLE_OUTPUT.encode("utf-8"))
    assert payload["stats"].get("degraded") is None


async def test_order_paid_degrades_gracefully_when_build_raises(monkeypatch):
    # AC4/Story 2.6: `verify` "never raises out", so the inner except is belt-and-suspenders.
    # If the engine DOES raise (a programming error despite the contract), the order is NOT
    # dropped — Proov delivers a graceful `unverifiable` deliverable so the order completes.
    import proov.engine as engine_mod

    def _boom(*_a, **_k):
        raise RuntimeError("verify exploded")

    monkeypatch.setattr(engine_mod, "verify", _boom)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-degrade"))
    await _drain(adapter)

    # Delivered (not dropped, not rejected), and the payload is the degraded report.
    assert len(client.deliver_calls) == 1
    assert client.reject_order_calls == []
    _, req = client.deliver_calls[0]
    payload = json.loads(req.deliverable_schema)
    assert payload["verdict"] == "unverifiable"
    assert payload["stats"].get("degraded") is True
    # Receipt stays real & reproducible in the degraded deliverable too (strip both
    # siblings — `receipt` and the Story 1.6 `verified_by_proov`).
    body = {k: v for k, v in payload.items() if k not in ("receipt", "verified_by_proov")}
    assert payload["receipt"]["report_hash"] == keccak256_hex(canonical_json(body).encode("utf-8"))


async def test_order_paid_rejects_terminally_when_even_graceful_build_fails(monkeypatch):
    # Review-patch (Story 1.6): if the GRACEFUL degrade build also fails (a deterministic
    # fault — both builders share the badge/receipt/canonical code), we must NOT fall to the
    # outer except's discard-and-retry (poison-message loop). The honest terminal action is
    # reject_order (escrow refunds), and a re-emit must not retry the same failure.
    import proov.deliverable as deliverable_mod
    import proov.engine as engine_mod

    def _boom(*_a, **_k):
        raise RuntimeError("build exploded")

    # Story 2.6 seam: the engine raises (inner try), AND the graceful backstop also raises
    # (its deterministic fault) → outer except → reject_order.
    monkeypatch.setattr(engine_mod, "verify", _boom)
    monkeypatch.setattr(deliverable_mod, "build_graceful_deliverable", _boom)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    artifact = await adapter._handle_order_paid("ord-fatal")

    # Rejected (refund), never delivered, no artifact returned.
    assert artifact is None
    assert client.deliver_calls == []
    assert len(client.reject_order_calls) == 1
    assert client.reject_order_calls[0][0] == "ord-fatal"

    # Terminal: a re-emitted order_paid must NOT retry (no second reject).
    artifact2 = await adapter._handle_order_paid("ord-fatal")
    assert artifact2 is None
    assert len(client.reject_order_calls) == 1


async def test_order_paid_infra_failure_on_deliver_is_swallowed_and_not_double_handled(caplog):
    # AC4: an infra failure on deliver itself (delivery channel down) CANNOT degrade-to-
    # partial — it stays logged-and-swallowed and rolls back so a re-emit retries.
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.deliver_error = RuntimeError("delivery channel down")
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    with caplog.at_level(logging.ERROR, logger="proov.provider"):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-infra"))
        await _drain(adapter)

    assert any("failed to deliver order" in r.getMessage() for r in caplog.records)
    # Rolled back → re-emit retries (delivers attempted again).
    client.deliver_error = None
    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-infra"))
    await _drain(adapter)
    assert len(client.deliver_calls) == 2


async def test_paid_reject_is_terminal_no_double_reject_on_reemit():
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.requirements = "not-json{"
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    evt = _event(type=EventType.ORDER_PAID, order_id="ord-reject-once")
    stream.dispatch(evt)
    await _drain(adapter)
    stream.dispatch(evt)  # re-emit must NOT re-reject after a successful reject
    await _drain(adapter)

    assert len(client.reject_order_calls) == 1


async def test_paid_reject_transient_failure_rolls_back_for_retry():
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.requirements = "not-json{"
    client.reject_order_error = RuntimeError("reject call flaked")
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-reject-retry"))
    await _drain(adapter)
    # First reject failed transiently → guard rolled back → re-emit retries the reject.
    client.reject_order_error = None
    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-reject-retry"))
    await _drain(adapter)

    assert len(client.reject_order_calls) == 2


async def test_order_paid_delivers_at_most_once():
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    evt = _event(type=EventType.ORDER_PAID, order_id="ord-dup")
    stream.dispatch(evt)
    await _drain(adapter)
    stream.dispatch(evt)
    await _drain(adapter)

    assert client.deliver_calls and len(client.deliver_calls) == 1


async def test_deliver_error_is_logged_and_does_not_crash(caplog):
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.deliver_error = RuntimeError("boom delivering")
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    with caplog.at_level(logging.ERROR, logger="proov.provider"):
        # Dispatch must not raise out of the sync handler / read loop.
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-err"))
        await _drain(adapter)

    assert any("failed to deliver order" in r.getMessage() for r in caplog.records)
    # Guard rolled back on failure, so a re-emit retries (delivers again).
    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-err"))
    await _drain(adapter)
    assert len(client.deliver_calls) == 2


async def test_get_order_error_is_logged_and_does_not_crash(caplog):
    stream = FakeEventStream()
    client = FakeClient(stream)
    client.get_order_error = RuntimeError("boom fetching")
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    with caplog.at_level(logging.ERROR, logger="proov.provider"):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-x"))
        await _drain(adapter)

    assert any("failed to deliver order" in r.getMessage() for r in caplog.records)
    assert client.deliver_calls == []  # never reached deliver


async def test_watchdog_trips_shutdown_and_run_raises_on_policy_violation(caplog):
    stream = FakeEventStream()
    # Mirror the SDK's actual 1008 error text.
    stream.set_err(
        RuntimeError("croo: websocket policy violation: duplicate SDK-Key connection")
    )
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client, watchdog_interval=0.01)

    with caplog.at_level(logging.ERROR, logger="proov.provider"):
        with pytest.raises(FatalProviderError):
            await asyncio.wait_for(adapter.run(), timeout=2.0)

    # Shutdown was triggered, stream + client closed, and a clear duplicate-key message logged.
    assert adapter._shutdown.is_set()
    assert stream.closed is True
    assert client.closed is True
    assert any("already connected with this key" in r.message for r in caplog.records)


async def test_graceful_shutdown_awaits_stream_close():
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client, watchdog_interval=10.0)

    # Pre-request shutdown so run() connects, registers, then exits cleanly.
    adapter.request_shutdown()
    await asyncio.wait_for(adapter.run(), timeout=2.0)

    # No fatal error on a clean shutdown; sockets closed gracefully.
    assert adapter._fatal_error is None
    assert stream.closed is True
    assert client.closed is True


# ---------------------------------------------- Metrics ledger record hook (Story 3.2)


async def test_delivered_order_is_recorded_to_ledger(monkeypatch):
    # A successful delivery records the expected OrderRecord (order_id/tier/status/counterparty/
    # wallet/price/cost) to the injected ledger; status is the delivery-time snapshot.
    import proov.ledger as ledger_mod

    monkeypatch.delenv("PROOV_QUICK_COST_USD", raising=False)
    fake = FakeLedger()
    monkeypatch.setattr(ledger_mod, "get_order_ledger", lambda: fake)

    stream = FakeEventStream()
    client = FakeClient(stream)
    client.order_requester_agent_id = "buyer-agent"
    client.order_requester_wallet_address = "0xbuyer"
    client.order_price = "100000"  # 0.10 USDC in base units (6 decimals)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-led"))
    await _drain(adapter)

    assert len(client.deliver_calls) == 1  # delivered
    assert len(fake.records) == 1
    rec = fake.records[0]
    assert rec.order_id == "ord-led"
    assert rec.tier == "quick"
    assert rec.status == "delivering"  # the snapshot — CLEAR/"completed" is async
    assert rec.requester_agent_id == "buyer-agent"
    assert rec.requester_wallet_address == "0xbuyer"
    assert rec.price_usd == 0.10  # base units scaled to USD
    assert rec.cost_usd == 0.0  # documented free-tier estimate (Story 3.4 measures it)


async def test_ledger_failure_does_not_break_delivery(monkeypatch, caplog):
    # NFR3 "degrade, don't drop": a ledger that RAISES on record must not drop/reject the order
    # nor trip the outer except — the order still delivers and returns its artifact unchanged.
    import proov.ledger as ledger_mod

    fake = FakeLedger(raise_on_record=True)
    monkeypatch.setattr(ledger_mod, "get_order_ledger", lambda: fake)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    with caplog.at_level(logging.ERROR, logger="proov.provider"):
        artifact = await adapter._handle_order_paid("ord-led-fail")

    assert len(client.deliver_calls) == 1  # still delivered
    assert client.reject_order_calls == []  # not rejected
    assert artifact is not None  # the tx-bearing artifact is unchanged
    assert "ord-led-fail" in adapter._delivered_orders  # idempotency guard stays set
    # The outer except (which logs "failed to deliver") was NOT tripped.
    assert not any("failed to deliver order" in r.getMessage() for r in caplog.records)


async def test_rejected_order_is_recorded_with_rejected_status(monkeypatch):
    # A malformed paid order is reject_order'd (escrow refund) AND recorded with status="rejected".
    import proov.ledger as ledger_mod

    fake = FakeLedger()
    monkeypatch.setattr(ledger_mod, "get_order_ledger", lambda: fake)

    stream = FakeEventStream()
    client = FakeClient(stream)
    client.requirements = "not-json{"
    adapter = ProviderAdapter(_cfg(), client=client)
    await adapter.start()

    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-rej-led"))
    await _drain(adapter)

    assert len(client.reject_order_calls) == 1
    assert len(fake.records) == 1
    assert fake.records[0].order_id == "ord-rej-led"
    assert fake.records[0].status == "rejected"


async def test_secret_key_never_appears_in_logs(caplog):
    stream = FakeEventStream()
    adapter = ProviderAdapter(_cfg(), client=FakeClient(stream))

    with caplog.at_level(logging.DEBUG):
        await adapter.start()

    assert all(_DUMMY_KEY not in r.getMessage() for r in caplog.records)


# ---------------------------------------------- Reliability hardening (Story 3.3)


async def _wait_until(predicate, *, tries: int = 200) -> None:
    """Yield the event loop until `predicate()` is true (deterministic, no wall-clock sleep)."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)


def test_resolve_max_concurrent_orders_hardening():
    # default 3; garbage / non-int / ≤0 → default; a valid value is honoured.
    assert _resolve_max_concurrent_orders(None) == 3
    assert _resolve_max_concurrent_orders("5") == 5
    assert _resolve_max_concurrent_orders("nonsense") == 3
    assert _resolve_max_concurrent_orders("0") == 3
    assert _resolve_max_concurrent_orders("-2") == 3


def test_resolve_shutdown_drain_seconds_hardening():
    # default 25; garbage / non-finite / ≤0 → default; a valid value is honoured.
    assert _resolve_shutdown_drain_seconds(None) == 25.0
    assert _resolve_shutdown_drain_seconds("10") == 10.0
    assert _resolve_shutdown_drain_seconds("nonsense") == 25.0
    assert _resolve_shutdown_drain_seconds("0") == 25.0
    assert _resolve_shutdown_drain_seconds("inf") == 25.0
    assert _resolve_shutdown_drain_seconds("nan") == 25.0


def test_resolve_idempotency_max_hardening():
    # default 4096; garbage / non-int / ≤0 → default; a valid value is honoured.
    assert _resolve_idempotency_max(None) == 4096
    assert _resolve_idempotency_max("100") == 100
    assert _resolve_idempotency_max("nonsense") == 4096
    assert _resolve_idempotency_max("0") == 4096
    assert _resolve_idempotency_max("-1") == 4096


def test_bounded_id_set_evicts_oldest_over_cap():
    # Push > cap ids: size stays ≤ cap, the OLDEST are evicted, the most-recent are kept.
    s = _BoundedIdSet(3)
    for i in range(5):
        s.add(f"id{i}")
    assert len(s) == 3
    assert "id0" not in s and "id1" not in s  # oldest two evicted
    assert "id2" in s and "id3" in s and "id4" in s  # most-recent kept
    # discard removes membership (the rollback-on-transient semantic).
    s.discard("id4")
    assert "id4" not in s


def test_bounded_id_set_re_add_refreshes_recency():
    # Re-adding an existing id moves it to the most-recent end, so it is NOT the next evicted.
    s = _BoundedIdSet(2)
    s.add("a")
    s.add("b")
    s.add("a")  # refresh "a" → "b" is now the oldest
    s.add("c")  # evicts the oldest = "b"
    assert "a" in s and "c" in s
    assert "b" not in s


async def test_worker_pool_bounds_concurrent_verifications(monkeypatch):
    # AC1: with max_concurrent_orders=1 and 3 paid orders whose verification blocks on a shared
    # Event, at most ONE order is in `engine.verify` at a time; the rest queue on the semaphore.
    # Release → all three complete and deliver. Proven with a counter around an awaited gate, not
    # timing.
    import proov.engine as engine_mod

    gate = asyncio.Event()
    state = {"concurrent": 0, "max": 0}
    real_verify = engine_mod.verify

    async def _gated_verify(inp, tier, **kwargs):
        state["concurrent"] += 1
        state["max"] = max(state["max"], state["concurrent"])
        try:
            await gate.wait()
            return await real_verify(inp, tier, **kwargs)
        finally:
            state["concurrent"] -= 1

    monkeypatch.setattr(engine_mod, "verify", _gated_verify)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client, max_concurrent_orders=1)
    await adapter.start()

    for oid in ("o1", "o2", "o3"):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id=oid))

    # All three tasks alive, exactly one inside verify (holding the only slot), two queued.
    await _wait_until(lambda: len(adapter._tasks) == 3 and state["concurrent"] == 1)
    assert state["max"] == 1
    assert state["concurrent"] == 1

    gate.set()
    await _drain(adapter)

    assert len(client.deliver_calls) == 3  # all delivered after release
    assert state["max"] == 1  # never more than one in verify at once
    assert state["concurrent"] == 0


async def test_worker_pool_allows_two_when_sized_two(monkeypatch):
    # AC1: max_concurrent_orders=2 lets exactly two run their verification at once (the third
    # queues) — proves the semaphore SIZE is the bound, not an accidental serialisation.
    import proov.engine as engine_mod

    gate = asyncio.Event()
    state = {"concurrent": 0, "max": 0}
    real_verify = engine_mod.verify

    async def _gated_verify(inp, tier, **kwargs):
        state["concurrent"] += 1
        state["max"] = max(state["max"], state["concurrent"])
        try:
            await gate.wait()
            return await real_verify(inp, tier, **kwargs)
        finally:
            state["concurrent"] -= 1

    monkeypatch.setattr(engine_mod, "verify", _gated_verify)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client, max_concurrent_orders=2)
    await adapter.start()

    for oid in ("o1", "o2", "o3"):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id=oid))

    await _wait_until(lambda: len(adapter._tasks) == 3 and state["concurrent"] == 2)
    assert state["max"] == 2  # two in verify, one queued

    gate.set()
    await _drain(adapter)
    assert len(client.deliver_calls) == 3
    assert state["max"] == 2  # never exceeded the bound


async def test_delivered_orders_guard_is_bounded():
    # AC5: with idempotency_max=2, three distinct delivered orders keep the guard at ≤ cap and
    # evict the oldest — while all three still deliver (eviction never blocks a NEW order).
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client, idempotency_max=2)
    await adapter.start()

    for oid in ("a", "b", "c"):
        stream.dispatch(_event(type=EventType.ORDER_PAID, order_id=oid))
        await _drain(adapter)

    assert len(adapter._delivered_orders) == 2  # bounded to the cap
    assert "a" not in adapter._delivered_orders  # oldest evicted
    assert "b" in adapter._delivered_orders and "c" in adapter._delivered_orders
    assert len(client.deliver_calls) == 3  # all three still delivered


async def test_watchdog_does_not_trip_on_recoverable_drop():
    # AC3: a recoverable drop (the SDK auto-reconnected, so `err()` stays None) is NOT treated as
    # fatal — the watchdog polls, sees no error, and a clean signal-driven shutdown returns with
    # no FatalProviderError. (The fatal-1008 trip is covered by the policy-violation test above;
    # the SDK owns reconnect — Proov only relies on it + the watchdog.)
    stream = FakeEventStream()  # err() returns None throughout (a healthy / recovered stream)
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client, watchdog_interval=0.01)

    run_task = asyncio.create_task(adapter.run())
    await _wait_until(lambda: bool(stream.on_calls))
    # Let the watchdog poll a few times seeing a None err() (no fatal).
    for _ in range(5):
        await asyncio.sleep(0)
    assert adapter.fatal_error is None
    adapter.request_shutdown()
    await asyncio.wait_for(run_task, timeout=2.0)
    assert adapter.fatal_error is None  # a recovered drop never became fatal
    assert stream.closed is True


async def test_shutdown_drain_awaits_inflight_deliver(monkeypatch):
    # AC4: an in-flight order task gated on an Event is AWAITED to completion within the drain
    # budget — the live deliver finishes instead of being abandoned. Proven with a gated task,
    # not a real wait.
    import proov.engine as engine_mod

    gate = asyncio.Event()
    real_verify = engine_mod.verify

    async def _gated_verify(inp, tier, **kwargs):
        await gate.wait()
        return await real_verify(inp, tier, **kwargs)

    monkeypatch.setattr(engine_mod, "verify", _gated_verify)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(
        _cfg(), client=client, watchdog_interval=10.0, shutdown_drain_seconds=5.0
    )

    run_task = asyncio.create_task(adapter.run())
    await _wait_until(lambda: bool(stream.on_calls))
    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-drain"))
    await _wait_until(lambda: bool(adapter._tasks))
    assert adapter._tasks  # one in-flight task gated mid-verify

    adapter.request_shutdown()  # run() reaches the drain and awaits the gated task
    await asyncio.sleep(0)
    gate.set()  # release it so it finishes WITHIN the drain budget
    await asyncio.wait_for(run_task, timeout=2.0)

    assert len(client.deliver_calls) == 1  # the in-flight deliver finished (not abandoned)
    assert stream.closed is True and client.closed is True


async def test_shutdown_drain_cancels_straggler(monkeypatch):
    # AC4: a task that never completes is CANCELLED after a tiny drain budget — shutdown does not
    # hang. Proven with a never-completing gated task + a tiny injected drain budget.
    import proov.engine as engine_mod

    never = asyncio.Event()

    async def _hang_verify(inp, tier, **kwargs):
        await never.wait()  # never set → the task never completes on its own

    monkeypatch.setattr(engine_mod, "verify", _hang_verify)

    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(
        _cfg(), client=client, watchdog_interval=10.0, shutdown_drain_seconds=0.05
    )

    run_task = asyncio.create_task(adapter.run())
    await _wait_until(lambda: bool(stream.on_calls))
    stream.dispatch(_event(type=EventType.ORDER_PAID, order_id="ord-hang"))
    await _wait_until(lambda: bool(adapter._tasks))
    inflight = next(iter(adapter._tasks))

    adapter.request_shutdown()
    await asyncio.wait_for(run_task, timeout=2.0)  # does NOT hang

    assert inflight.cancelled()  # the straggler was cancelled after the budget
    assert client.deliver_calls == []  # never delivered (was hung, then cancelled)
    assert stream.closed is True and client.closed is True


async def test_handle_order_paid_without_start_runs_without_throttle():
    # AC1 defensive: a direct `_handle_order_paid` call that skipped `start()` (so `_sem` is
    # None) must still deliver — `_verification_slot` degrades to no throttle rather than crash.
    stream = FakeEventStream()
    client = FakeClient(stream)
    adapter = ProviderAdapter(_cfg(), client=client)  # NOTE: no start() → self._sem is None
    assert adapter._sem is None

    artifact = await adapter._handle_order_paid("ord-no-start")
    assert artifact is not None
    assert len(client.deliver_calls) == 1

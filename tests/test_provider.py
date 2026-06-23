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


async def test_secret_key_never_appears_in_logs(caplog):
    stream = FakeEventStream()
    adapter = ProviderAdapter(_cfg(), client=FakeClient(stream))

    with caplog.at_level(logging.DEBUG):
        await adapter.start()

    assert all(_DUMMY_KEY not in r.getMessage() for r in caplog.records)

# Proov

**Proof-of-Verification Oracle on the CROO Agent Store.**

A paid, callable CAP agent that verifies the factual claims in an AI-generated output against real sources and returns a verdict, an evidence trail, and a tamper-proof on-chain verification receipt.

- **Network:** Base mainnet (chain 8453) · payments in USDC · gas sponsored by the CROO Paymaster
- **Services:** Quick Check ($0.10, SLA <5 min) · Deep Verify ($0.50, SLA <30 min)
- **Stack:** Python 3.10+ · `croo-sdk` · pluggable LLM/search · SQLite cache

## Setup

> Filled in during Story 1.1 (register agent, configure services, secure API key, secure test funds).

Environment variables (see `.env.example`):

```
CROO_API_URL=https://api.croo.network
CROO_WS_URL=wss://api.croo.network/ws
CROO_API_KEY=croo_sk_...
```

Copy `.env.example` to `.env` and fill in your `croo_sk_...` API key (issued once in the Agent Store dashboard). **Never commit `.env`.**

Install: `pip install -e .` (Python 3.10+). The CROO SDK is published as **`croo-sdk`** on PyPI but its import name is **`croo`** (not `croo_sdk`):

```python
from croo import AgentClient
```

> **SDK does not read env vars.** Unlike the dashboard's TypeScript quickstart, the
> Python SDK takes config explicitly: `Config(base_url, ws_url, rpc_url)` and
> `AgentClient(config, sdk_key=...)`. Our code reads the vars above from `.env` and
> passes them in. `CROO_API_KEY` is named to match the dashboard (the SDK arg is `sdk_key`).

### Agent registration (Story 1.1)

- **Name:** Proov · **Skill tags:** Research & Report, Data & Analytics
- **Services** (flat per-call price, *Require Fund Transfer = off*):

  | Service      | Service ID                                | Price (USDC) | SLA | requirements / deliverable |
  |--------------|-------------------------------------------|--------------|-----|----------------------------|
  | Quick Check  | `a31ee562-142f-44c8-88b9-a5991874792f`    | 0.10         | 5m  | Schema / Schema            |
  | Deep Verify  | `b8e4a546-69c4-42f5-b21f-087daa2333d0`    | 0.50         | 30m | Schema / Schema            |

  > **Service-ID note (2026-06-21):** CROO mints a new service id on every (re)registration and switched its id format from `svc-new-<digits>` to **UUIDs** — the original `svc-new-…` ids are dead (`SERVICE_NOT_FOUND`). Quick Check's current UUID above was confirmed via a real paid order; Deep Verify's current UUID was copied from the dashboard 2026-06-21.

- **Proov (Provider) AA wallet:** `0xaA516fe81F4ea22735DFdE66A0a00dA080b06cBc`
- **Requester (test buyer) AA wallet:** `0x30e9d4638E0B765F0c5F5F8af19b185b3B850296`
- **DID:** minted at registration as an **ERC-8004 sovereign-identity NFT** (per CROO docs). **Confirmed not displayed anywhere on the dashboard (including the Configure page)**, and the Python SDK (`croo` 0.2.1) cannot read it. Treated as unavailable for now — would require the Go SDK / REST agent-details or an on-chain ERC-8004 registry lookup. _DID value: unavailable via current tooling._
- **Agent status — both agents are `draft` (expected for Story 1.1).** Per CROO docs, an agent is only **Visible in Store** when `online`, and it transitions `draft → online` **automatically when the SDK completes the WebSocket handshake** (the provider loop). That provider code is **Story 1.2**, out of scope here. So full Store discoverability (AC1) is realized in Story 1.2; Story 1.1 covers registration + service/schema config + secure key storage.
- **Input/output schemas — CONFIRMED DEVIATION (2026-06-19):** both services are set to **Schema** for requirements + deliverable, but the **live dashboard exposes no field-level schema builder** — neither at create-time nor on the Configure page (docs describe one, the deployed UI does not have it). So only the *type* ("Schema (JSON)") is publishable; the field structure is **not** entered on-dashboard. Authoritative field contract lives here + in **PRD §6**, and the runtime engine (Epic 2) emits the full JSON regardless:
  - **Input (requirements):** `output` *(string, required)*, `claims` *(string[])*, `sources` *(object[]: `url`, `title`)*, `mode` *(string: quick|deep)*, `options` *(object: `max_claims` number, `language` string)*.
  - **Output (deliverable):** `verdict` *(string: pass|fail|partial|unverifiable)*, `confidence` *(number)*, `summary` *(string)*, `claims` *(object[])*, `citations_checked` *(object[])*, `stats` *(object)*, `receipt` *(object: output_hash, report_hash, verdict, confidence, model, version, timestamp, anchor_ref)*, `disclaimer` *(string)*.
- **Test funding (Task 3) — FUNDED & PROVEN (2026-06-21):** Requester wallet `0x30e9…0296` funded with USDC on Base (chain 8453), resolving Story 1.1 AC2. Proven by a **real paid order** end-to-end: order `2c4ac135-ef8a-4162-9396-4088cfb06854`, **pay tx `0x1847a9357aaa0b649ba0017edae2c7a227cc5e453a4c4318831f568539c2933e`**, delivery `18e714cd-ca5a-45e8-aa7a-c37908b1a16d` → `completed`. _(Funding tx hash itself: `<optional — paste if recording the USDC top-up tx>`.)_
- **API keys:** stored only in untracked `.env` (`CROO_API_KEY` = Proov, `CROO_REQUESTER_API_KEY` = Requester). The Requester key was rotated on 2026-06-19 (old key invalidated); `.env` holds the current value. Keys are never committed.

## Run the provider (Story 1.2)

The provider process opens a persistent WebSocket to CROO, **goes `online`**, and listens for events. Running it is what flips Proov `draft → online` in the dashboard — which is also what completes Story 1.1's AC1 (Store discoverability).

```bash
pip install -e .          # runtime (Python 3.10+); add ".[test]" for the test deps
python -m proov           # connects, goes online, listens — Ctrl-C to stop
```

**Required env vars** (from `.env`, see `.env.example`):

```
CROO_API_URL=https://api.croo.network
CROO_WS_URL=wss://api.croo.network/ws
CROO_API_KEY=croo_sk_...        # Proov provider key
```

`LOG_LEVEL` (default `INFO`) also controls the SDK's `croo` logger so reconnect/heartbeat lines surface. Config loads `.env` without overwriting any var already set in the real environment, and **fails fast** (exit 1) naming the first missing required var — the key value is never logged.

**Expected log lines on a healthy start:**

```
... proov.provider: provider online: listening for events
... proov.provider: event received: type=... order_id=... negotiation_id=...   (when events arrive)
```

**One provider per key.** A second `python -m proov` on the same `CROO_API_KEY` is rejected by the server with WS code `1008` (policy violation); the SDK stops reconnecting and our watchdog logs *"another provider is already connected with this key"* and **exits non-zero**. Never run two instances on one key (relevant for single-instance deployment).

**Reconnect & heartbeat are SDK-managed** (30s ping, 60s pong-timeout, exponential backoff). Our adapter only keeps the process alive, surfaces the fatal duplicate-key case, and shuts down gracefully on `SIGINT`/`SIGTERM`.

### Live smoke test (manual — validates AC1 & AC2)

1. `python -m proov` and confirm the log shows `provider online: listening for events`.
2. Open the CROO dashboard and confirm **Proov status = `online`** with an active heartbeat. *(This is the authoritative `online` signal; the Python SDK 0.2.1 cannot read agent status, so the dashboard is the source of truth.)*
3. Simulate a network drop (toggle Wi-Fi / pull the network for a few seconds), restore it, and confirm the logs show the SDK **reconnecting** and the dashboard returns to `online` — **with no manual restart**.

> Automated tests cover config parsing and the adapter's wiring/lifecycle with a fake `EventStream` (no real socket). True `online` status (AC1) and live reconnect (AC2) are validated by this manual smoke test, since they depend on the running platform.

## Order happy path (Story 1.3)

The provider now drives a full CAP transaction end to end:

1. On `order_negotiation_created`, Proov calls **`accept_negotiation(negotiation_id)`** (the plain accept — Proov's services are *Require Fund Transfer = off*, so the fund-address variant is never used). The backend's dual-sig `createOrder` fires and both parties get `order_created`.
2. On `order_paid`, Proov fetches the order, builds a **schema-valid stub deliverable** (PRD §6 shape), and calls **`deliver_order(order_id, DeliverOrderRequest(deliverable_type=SCHEMA, deliverable_schema=<json>))`**.
3. CAP writes `keccak256(deliverable)` on-chain and settlement releases automatically (price − platform fee → Proov's AA wallet `0xaA51…6cBc`). The order reaches **`completed`**; Proov confirms this from the `deliver_order` **return value** (`result.order.status == "completed"`), **not** a WS event — `order_completed` is pushed to the *Requester*, not the Provider.

Order work is offloaded from the synchronous WS handlers via `asyncio.create_task` and is **idempotent per id** (a negotiation is accepted at most once, an order delivered at most once); a handler error is logged and swallowed so the read loop never crashes.

> **Scope boundary.** This story is the **happy path only**. The deliverable here is an explicit **stub** (`verdict: "unverifiable"`) — the real verification engine is **Epic 2**. The on-chain **receipt** fields + re-hash verification procedure are now **delivered in Story 1.4** (see ["Verify a receipt independently"](#verify-a-receipt-independently-story-14) below) — the *verdict* stays a stub until Epic 2, and the reusable "Verified by Proov" artifact (in-band badge + post-delivery tx-bearing form) is now **delivered in Story 1.6** (see ["'Verified by Proov' artifact"](#verified-by-proov-artifact-story-16) below). Input validation + the two reject stages (negotiation-stage `reject_negotiation`, paid-stage `reject_order`), the graceful `partial`/`unverifiable` degrade on engine error, and the SLA-timeout-refund documentation are now **delivered in Story 1.5** (see ["Input validation & graceful failure"](#input-validation--graceful-failure-story-15) below). The bounded worker-pool concurrency throttle is still **Epic 3 (Story 3.3)**, and per-order timeout enforcement is **Epic 2 / Story 3.3**.

### Happy-path smoke test (manual — validates AC3 & AC5)

The Requester harness `scripts/place_test_order.py` negotiates + pays a real order to drive the path live. It uses `CROO_REQUESTER_API_KEY` (the test buyer key, distinct from Proov's — so it does **not** trip the one-WS-per-key 1008 rule).

1. **Provider online:** `python -m proov` — confirm `provider online: listening for events`.
2. **Fund the Requester wallet:** send a few cents of USDC on Base (chain 8453) to `0x30e9d4638E0B765F0c5F5F8af19b185b3B850296`. **`pay_order` does an on-chain balance pre-check and raises `InsufficientBalanceError` if the wallet is empty.**
3. **Place the order:** `python scripts/place_test_order.py [svc-id]` (defaults to Quick Check `a31ee562-142f-44c8-88b9-a5991874792f`).
4. **Observe:** the provider logs `negotiation accepted ...` then `order delivered ... status=completed`; the harness prints the delivered schema; Proov's AA wallet receives settlement. **Record the deliver/clear `tx_hash` as evidence.**

> **Funding gate — RESOLVED (2026-06-21).** The Requester wallet `0x30e9…0296` is now funded with USDC on Base (Story 1.1 AC2), so the live accept→pay→deliver→settle smoke test above is **unblocked**. (There is no Base testnet — settlement is real money, so each Quick Check run spends ~$0.10 USDC.) The harness is **test tooling**, not product, and lives outside the `proov` package so it isn't shipped. The automated suite still runs entirely on a fake SDK client (`pytest`) and never touches the live platform.

> **First live order — PASSED (2026-06-21).** Quick Check service `a31ee562-142f-44c8-88b9-a5991874792f`, order `2c4ac135-ef8a-4162-9396-4088cfb06854` (negotiation `7716a60b-76c9-4f19-b939-24c4fa0f524a`, delivery `18e714cd-ca5a-45e8-aa7a-c37908b1a16d`). Full on-chain trail:
>
> | Step | Tx hash |
> |------|---------|
> | pay (Requester) | `0x1847a9357aaa0b649ba0017edae2c7a227cc5e453a4c4318831f568539c2933e` |
> | deliver (Proov) | `0x544536998c7a3f5a486c01ecaddd9664fff03d9dfbad117611c529c2ee5affc2` |
> | clear / settle | `0x7676395361a4980e6af149430b91a5d66bafe4bacde54382e5d3b5543791cb5f` |
>
> **On-chain deliverable anchor (`content_hash`, used in Story 1.4):** `0xadedb261d3ca8bf65554f2b3a7e775d9e0f95b33f660bfad6611bf072434b6b3`. Requester received the schema-valid stub deliverable (`verdict: "unverifiable"`, engine = Epic 2).
>
> **Settlement verified on-chain** (decoded from the clear tx): of the $0.10 USDC paid, **0.09 USDC → Proov AA wallet `0xaA51…6cBc`** and **0.01 USDC → platform Treasury `0x49f3…9f51` (10% fee)**. AC3 confirmed.
>
> **Two findings:** (1) **CLEAR is asynchronous** — `deliver_order` returns with `status=delivering`, and the order reaches `completed` ~1 min later; confirm via `get_order().clear_tx_hash`, never by blocking on the deliver return. (2) The Order `fee_amount` field reported `100000` (= full price) and is **unreliable** — the real fee was 0.01 USDC (10%) per the on-chain clear tx. Trust the chain, not that field.

## Verify a receipt independently (Story 1.4)

Every delivered verdict carries a tamper-evident, independently re-verifiable on-chain **receipt**. The deliverable JSON *is* the receipt: its `receipt` object holds real **Ethereum keccak256** hashes (`output_hash`, `report_hash`), the producing `model`/`version`/`timestamp`, and an `anchor_ref` descriptor. The whole deliverable is anchored on **Base mainnet** — the CAP backend computes `keccak256(deliverable)` and writes it on-chain in the deliver tx, returning it as `delivery.content_hash`. No custom contract; verification is native.

> **⚠️ keccak256 ≠ SHA3-256.** Base is an Ethereum L2 — the anchor is **Ethereum keccak256** (pre-NIST), *not* NIST FIPS-202 SHA3-256. Python's `hashlib.sha3_256` gives a **different** digest and will silently fail to match. Use a real keccak (we use `pycryptodome`: `from Crypto.Hash import keccak`). Sanity check: `keccak256("") == 0xc5d2460186f7233c…d85a470` (if you get `0xa7ffc6f8bf1ed766…` you have SHA3-256 — the wrong algorithm).

### The exact byte rule (empirically confirmed)

The on-chain `content_hash` is **`keccak256` of the exact UTF-8 bytes the provider POSTed** as `deliverableSchema`. The provider POSTs **canonical JSON** — sorted keys, no whitespace, raw unicode: `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

**Important subtlety:** `get_delivery(order_id).deliverable_schema` returns a *re-serialised* copy of the deliverable — the CAP backend reorders the JSON keys and decodes unicode escapes — so hashing the returned string **verbatim does not reproduce `content_hash`**. Because the delivered form is canonical (order-independent), a verifier reproduces the anchor by **re-canonicalising** the returned object first:

```python
import json
from Crypto.Hash import keccak

def keccak256_hex(b: bytes) -> str:
    return "0x" + keccak.new(digest_bits=256, data=b).hexdigest()

def canonical_json(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

delivery = await client.get_delivery(order_id)              # deliverable_schema + content_hash
obj = json.loads(delivery.deliverable_schema)
assert keccak256_hex(canonical_json(obj).encode("utf-8")) == delivery.content_hash  # (1) tamper-evident
```

### Step-by-step

1. **Fetch the delivery:** `get_delivery(order_id)` → `deliverable_schema` (the stored JSON) and `content_hash` (the on-chain anchor).
2. **Tamper-evidence:** `keccak256(canonical_json(json.loads(deliverable_schema))) == content_hash`. If anyone altered a byte of the result after Proov produced it, this fails. (`scripts/probe_anchor.py` reproduces a known live anchor; against the pre-1.4 reference order it uses the legacy original-bytes form, since that order predates canonical delivery — see the script header.)
3. **On-chain confirmation:** confirm `content_hash` is the value written on Base by the order's `deliver_tx_hash` (open the deliver tx on a [Base explorer](https://basescan.org)).
4. **Recompute the inner hashes** from the delivered JSON:
   - `output_hash` = `keccak256(utf8(<the input `output` text from the order's negotiation requirements>))`.
   - `report_hash` = `keccak256(utf8(canonical_json(deliverable **without** the `receipt` key)))`. (The receipt cannot hash a structure that contains itself, so `report_hash` excludes it.)

> **Why `anchor_ref` is not a tx hash.** The receipt lives *inside* the deliverable, and `content_hash = keccak256(whole deliverable incl. receipt)`. Writing the deliver tx / `content_hash` into the receipt would change the very bytes being hashed (circular) — so `anchor_ref` is a stable descriptor of *where* the anchor lives (`chain` / `mechanism` / `anchor_field`). The post-delivery, tx-bearing "Verified by Proov" artifact is **delivered in Story 1.6** (see ["'Verified by Proov' artifact"](#verified-by-proov-artifact-story-16) below) — and the in-band badge now lives *inside* the hashed deliverable, so the tamper-evidence re-hash above covers it too.

**Worked examples (live on Base mainnet).**

- **Pre-1.4 reference (Story 1.3) — pinned the byte rule.** Order `2c4ac135-ef8a-4162-9396-4088cfb06854`, anchor `content_hash = 0xadedb261d3ca8bf65554f2b3a7e775d9e0f95b33f660bfad6611bf072434b6b3`, deliver tx `0x544536998c7a3f5a486c01ecaddd9664fff03d9dfbad117611c529c2ee5affc2`. Its `receipt` was the `{}` placeholder; it was delivered with non-canonical `json.dumps`, so it reproduces only from its original bytes (`scripts/probe_anchor.py` Regime 2). *(AC3a)*
- **Fresh Story-1.4 order — full end-to-end verification.** Order `cfd6507f-9285-4085-b78c-5efee05d7b7f` (negotiation `d4602f68-436a-4e31-aff3-dd0aa33afec3`, delivery `460ac8f0-9fc5-4465-a065-5c569cb57d0c`, pay tx `0xb7927572ce78c67593d361fddf44d31bafa4f5d4cebac95f9cc03d0feba934d2`), anchor `content_hash = 0x12cfd3586fb0d2f3864fca95eacedab538377a9fe2c38b80fbdff17e5ba7f89d`. Delivered with a **populated receipt** and canonical JSON, so all three checks reproduce from `get_delivery` alone: `keccak256(canonical_json(returned)) == content_hash` (run `.venv/bin/python scripts/probe_anchor.py cfd6507f-9285-4085-b78c-5efee05d7b7f`), `output_hash == keccak256("The Eiffel Tower is located in Paris, France and was completed in 1889.")`, and `report_hash == keccak256(canonical_json(deliverable − receipt))`. *(AC3b)*

## "Verified by Proov" artifact (Story 1.6)

The on-chain receipt is the reusable currency: any agent that called Proov — and any
buyer/auditor reading a delivered order — gets a portable **"Verified by Proov" artifact**
that traces back to the anchored receipt (FR16). A pure, SDK-agnostic builder
(`proov/badge.py` → `build_verified_artifact`) derives the whole payload from a deliverable's
`receipt` (+ an optional concrete on-chain `anchor`) — no new runtime dependency, no I/O.

**Schema (`proov.verified-by-proov.v1`).** A self-contained JSON object:

```json
{ "issuer": "Proov",
  "schema": "proov.verified-by-proov.v1",
  "version": "0.1.0",
  "verdict": "unverifiable", "confidence": 0.0, "model": "stub-no-engine",
  "timestamp": "2026-06-21T…Z",
  "output_hash": "0x…", "report_hash": "0x…",
  "anchor_ref": { "chain": "base-mainnet", "mechanism": "cap-deliver-keccak256", "anchor_field": "content_hash" },
  "anchor": null,
  "receipt_id": "0x… (report_hash in-band, content_hash once anchored)",
  "verify": { "rule": "keccak256(canonical_json(json.loads(get_delivery(order_id).deliverable_schema))) == content_hash",
              "procedure": "README#verify-a-receipt-independently-story-14" } }
```

The `verdict`/`confidence`/`model` mirror the receipt — honest that the engine is still an
Epic 2 stub. The `verify` block points a third party at the [receipt verification
procedure](#verify-a-receipt-independently-story-14) above.

**Two forms.**

1. **In-band badge** — every delivered deliverable carries a top-level `verified_by_proov`
   object, a **sibling** of `receipt`. It has `anchor: null` and `receipt_id = report_hash`,
   because the deliver tx / `content_hash` are not yet known pre-delivery and embedding them
   would change the very bytes being hashed (the same circularity that keeps them out of the
   `receipt`). The badge is added *after* the receipt is computed over the unchanged report
   body, so **`report_hash` is unchanged** — but a verifier reproducing `report_hash` from
   the delivered object must now strip **both** `receipt` **and** `verified_by_proov` before
   re-canonicalising. The badge lives inside the hashed deliverable, so the Story 1.4
   tamper-evidence re-hash (`keccak256(canonical_json(deliverable)) == content_hash`) covers
   it too.
2. **Post-delivery, tx-bearing artifact** — assembled by the provider after `deliver_order`
   returns, when the on-chain result is known. It carries a concrete
   `anchor = { order_id, content_hash, deliver_tx_hash, delivery_id, chain: "base-mainnet",
   explorer_url }` and `receipt_id = content_hash` (the on-chain anchor is the canonical
   receipt id once known). The provider logs it as a `verified-by-proov artifact: …` evidence
   line and returns it from the order handler.

**Attaching it to your own delivery (FR16).** A caller agent that hired Proov takes the
returned tx-bearing artifact and embeds it in *its own* deliverable (e.g. a
`proof.verified_by_proov` field), so its buyer can trace the chain of verification: the
caller's deliverable → the embedded artifact → `anchor.content_hash` / `anchor.deliver_tx_hash`
on [Base](https://basescan.org) → the Proov receipt that re-hashes to it. The companion
Research caller that consumes the artifact this way is **Epic 4** — Story 1.6 produces and
exposes the artifact; it does not build the caller.

**Tracing it back.** Given any artifact, open `anchor.explorer_url` (or look up
`anchor.deliver_tx_hash` on Base) to see `content_hash` written on-chain, then run the
[receipt verification procedure](#verify-a-receipt-independently-story-14) over
`get_delivery(anchor.order_id)` to confirm the deliverable re-hashes to it. For the in-band
form (`anchor: null`), `receipt_id` is the `report_hash` you reproduce from the delivered
JSON (strip both siblings).

## Input validation & graceful failure (Story 1.5)

Proov never charges a buyer for nothing and never crashes on bad input. The submitted input is the negotiation's `requirements` JSON string (PRD §6 input contract):

```json
{ "output": "<required, non-empty string — the AI text to verify>",
  "claims":  ["<optional list of claim strings>"],
  "sources": [{ "url": "https://…", "title": "optional" }],
  "mode":    "quick | deep (advisory — the tier is authoritative from service_id)",
  "options": { "max_claims": 0, "language": "en" } }
```

A **pure, SDK-agnostic validator** (`proov/validation.py` → `validate_requirements`) checks this and returns either a normalised input or a **structured error** with a stable machine code. The byte-size cap is checked on the **raw string before `json.loads`**, so an oversized payload never materialises a huge object. Unknown extra keys, an odd `mode`, and extra `options` are **tolerated** (forward-compatible) — Proov rejects only clearly-malformed input, because a wrongly-rejected legitimate order costs a real buyer.

| `code` | meaning | rejected at |
|--------|---------|-------------|
| `output_too_large` | raw `requirements` exceeds the byte cap (default 256 KB, env `PROOV_MAX_INPUT_BYTES`) — checked **before** parse | negotiation / paid |
| `invalid_json` | not parseable JSON, or not a JSON object | negotiation / paid |
| `missing_output_field` | no `output` key | negotiation / paid |
| `output_not_string` | `output` is not a string | negotiation / paid |
| `empty_output_field` | `output` is blank/whitespace | negotiation / paid |
| `invalid_sources` | `sources` present but not a list of `{url}` objects | negotiation / paid |

**Two reject stages (defence in depth):**

1. **Negotiation stage — `reject_negotiation` (buyer never pays, primary).** On `order_negotiation_created`, Proov validates the negotiation's `requirements` *before* accepting. Malformed → `reject_negotiation(negotiation_id, "<code>: <detail>")`, so **no on-chain `createOrder` ever fires** and the buyer is never charged. Valid → `accept_negotiation` exactly as before.
2. **Paid stage — `reject_order` → auto-refund (defensive).** If a *paid* order's input fails validation (the negotiation gate was bypassed or slipped through), Proov calls `reject_order(order_id, "<code>: <detail>")` instead of delivering. The CAP escrow then **auto-refunds** the Requester (`paid → rejecting → rejected`). Only the Provider can reject a paid order.

**Graceful degrade (NFR3 — degrade, don't drop).** For a *valid* paid order, if the verification/build step raises an internal error, Proov still delivers a schema-valid deliverable with `verdict: "unverifiable"` (or `"partial"`), `confidence: 0.0`, an honest summary stating verification could not complete, and a real populated receipt — so the order reaches `completed` with value delivered rather than silently timing out. This `build_graceful_deliverable` path is the seam the Epic 2 `verify()` engine plugs into. (An infra failure that makes *delivery itself* impossible — the delivery channel is down — cannot degrade-to-partial; it is logged-and-swallowed and falls through to the SLA-timeout refund below.)

**SLA-timeout refunds are platform-automatic.** If Proov cannot deliver within an order's SLA, the CAP escrow **automatically refunds** the Requester when the deadline passes (`order → expired`, refund) — **no provider action, and the provider must never manually refund**. Proov's only responsibility is to not block forever and to degrade-to-deliver where it can. [Source: CROO Security & Trust Model — "Timeout triggers automatic refund with no manual intervention required".]

> **Live reject/refund is verifiable but kept manual.** The automated suite (`pytest`) exercises every branch on a fake SDK client and never touches the live platform. A live negotiation-stage reject costs the buyer **nothing** (no pay); a live paid-stage reject costs a real ~$0.10 USDC round-trip (refunded), so it stays a manual check, not part of CI.

## Claim extraction (Story 2.1)

The first slice of the verification engine: extract discrete, checkable factual **claims** from a submitted output, behind a **pluggable** `LLMProvider` interface so the model can be swapped without touching the engine. `proov/types.py` (pure) holds the `Claim`/`Tier` types and the per-tier caps; `proov/llm.py` holds the interface, providers, factory, and a provider-agnostic `extract_claims` entrypoint.

- **Pluggable by design.** `LLMProvider` is a `@runtime_checkable` `Protocol`; the only code that names a concrete provider is the `get_llm_provider` factory. Adding a provider means implementing `extract_claims` and registering it — no engine change (the epic's load-bearing AC).
- **Gemini 2.5 Flash (primary).** A thin raw-REST provider via `httpx` with structured-JSON output (no vendor SDK). The key is sent in the `x-goog-api-key` **header, never the `?key=` query param**, and is `register_secret`-ed so it can never leak into a log.
- **Caps (FR6).** Quick = 20 claims, Deep = default 50; a caller `options.max_claims` may **lower** the cap but never raise it above the tier ceiling.
- **Degrade, don't drop (NFR3).** A transport/status/timeout failure raises `LLMError` (→ the Story 1.5 graceful seam turns it into an honest `unverifiable`); a successful response that yields no parseable claims returns `[]` — an empty extraction is a valid outcome, not a crash.

**LLM env vars** (all optional for the offline suite — see below):

```
GEMINI_API_KEY=...              # required for the live Gemini provider (free key from Google AI Studio;
                                # GOOGLE_API_KEY is also accepted as a fallback)
PROOV_LLM_PROVIDER=gemini       # default `gemini`; set `stub` for the deterministic offline provider
PROOV_LLM_MODEL=gemini-2.5-flash
PROOV_LLM_TIMEOUT=30            # per-call timeout in seconds (garbage falls back to 30)
```

> **The suite runs fully offline and needs no key.** `StubLLMProvider` (deterministic, zero network) and a mocked `httpx.MockTransport` cover every path — no test opens a real socket or spends Gemini quota. A live Gemini smoke is kept manual.

## Evidence retrieval (Story 2.2)

The second slice of the verification engine: retrieve real, source-linked **evidence** for each extracted claim, behind a **pluggable** `SearchProvider` interface so the search backend can be swapped without touching the engine. `proov/types.py` (pure) gains the `Evidence` type and the per-tier evidence counts; `proov/search.py` holds the interface, providers, factory/chain, and a provider-agnostic `retrieve_evidence` entrypoint. It mirrors the shape of `proov/llm.py` beat-for-beat.

- **Pluggable by design.** `SearchProvider` is a `@runtime_checkable` `Protocol` declaring only `search(query, k)`; the only code that names a concrete provider is `get_search_provider` + `default_search_chain`. Adding a backend (e.g. Serper) means implementing `search` and registering it — no engine change.
- **Wikipedia (keyless, always-on fallback).** A thin raw-REST provider hitting the MediaWiki REST `search/page` endpoint via `httpx` (no key, no vendor SDK). The match-highlight HTML in each excerpt is stripped to a clean snippet.
- **Tavily (optional RAG-native primary).** A thin raw-REST `POST /search` via `httpx`. The key is sent in the `Authorization: Bearer` **header, never a URL/body**, and is `register_secret`-ed so it can never leak into a log. Free tier ≈ 1,000 searches/mo.
- **Fallback Tavily→Wikipedia, per-claim timeout (FR7).** `retrieve_evidence` tries each provider in order under a per-call `asyncio.wait_for` timeout; on a provider raising `SearchError` or timing out it falls through to the next, and if every provider fails it returns `[]` — the claim becomes `unverifiable` downstream (degrade, don't drop — NFR3). It never raises.
- **`Evidence` is the raw retrieved chunk only** (`source`/`title`/`snippet`/`score?`) — it carries no `stance`; stance is a *judgment* output (Story 2.3).

**Search env vars** (all optional for the offline suite):

```
TAVILY_API_KEY=...              # optional RAG-native primary (free key from tavily.com);
                                # without it, retrieval is Wikipedia-only (keyless)
PROOV_SEARCH_PROVIDER=          # force a single `wikipedia|tavily|stub`; unset = auto chain
                                # (Tavily→Wikipedia when keyed, else Wikipedia only)
PROOV_SEARCH_TIMEOUT=10         # per-call (per-claim) timeout in seconds (garbage falls back to 10)
```

> **The suite runs fully offline and needs no key.** `StubSearchProvider` (deterministic, zero network) and a mocked `httpx.MockTransport` cover every path — no test opens a real socket or spends Tavily/Wikipedia quota. No new runtime dependency: `httpx` (declared in Story 2.1) covers both providers — deliberately no `tavily-python`/`wikipedia` package (vendor lock-in the pluggable interface exists to avoid). A live smoke is kept manual.

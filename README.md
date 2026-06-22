# Proov

**Proof-of-Verification Oracle on the CROO Agent Store.**

A paid, callable CAP agent that verifies the factual claims in an AI-generated output against real sources and returns a verdict, an evidence trail, and a tamper-proof on-chain verification receipt.

- **Network:** Base mainnet (chain 8453) · payments in USDC · gas sponsored by the CROO Paymaster
- **Services:** Quick Check ($0.10, SLA <5 min) · Deep Verify ($0.50, SLA <30 min)
- **Stack:** Python 3.10+ · `croo-sdk` · pluggable LLM/search · SQLite cache

## Status

The CAP transaction path (negotiate → pay → deliver → settle), the tamper-evident on-chain
receipt, input validation and graceful degrade, and the pluggable claim-extraction and
evidence-retrieval layers are built and tested. The per-claim judgment and verdict
aggregation that turn retrieved evidence into a final verdict are in active development, so
deliveries currently return an honest `unverifiable` verdict with a fully populated,
independently verifiable receipt.

## How it works

A caller submits an AI-generated `output` (optionally with `claims` and `sources`). Proov:

1. **Extracts** discrete, checkable factual claims from the output (pluggable LLM).
2. **Retrieves** real, source-linked evidence for each claim (pluggable search).
3. **Judges** each claim against its evidence and **aggregates** a verdict *(in development)*.
4. **Delivers** a schema-valid result whose `receipt` is anchored on Base mainnet, so anyone
   can re-hash the deliverable and confirm it on-chain.

## Setup

Environment variables (see `.env.example`):

```
CROO_API_URL=https://api.croo.network
CROO_WS_URL=wss://api.croo.network/ws
CROO_API_KEY=croo_sk_...
```

Copy `.env.example` to `.env` and fill in your `croo_sk_...` API key (issued in the Agent
Store dashboard). **Never commit `.env`.**

Install (Python 3.10+):

```bash
pip install -e .          # runtime; add ".[test]" for the test deps
```

The CROO SDK is published as **`croo-sdk`** on PyPI but its import name is **`croo`**:

```python
from croo import AgentClient
```

> **The Python SDK does not read env vars.** Unlike the dashboard's TypeScript quickstart, it
> takes config explicitly: `Config(base_url, ws_url, rpc_url)` and `AgentClient(config,
> sdk_key=...)`. Proov reads the vars above from `.env` and passes them in. `CROO_API_KEY` is
> named to match the dashboard (the SDK arg is `sdk_key`).

## Running the provider

The provider process opens a persistent WebSocket to CROO, goes **online**, and listens for
order events. Running it is what makes Proov discoverable in the Store.

```bash
python -m proov           # connects, goes online, listens — Ctrl-C to stop
```

Required env vars (from `.env`):

```
CROO_API_URL=https://api.croo.network
CROO_WS_URL=wss://api.croo.network/ws
CROO_API_KEY=croo_sk_...        # Proov provider key
```

`LOG_LEVEL` (default `INFO`) also controls the SDK's `croo` logger. Config loads `.env`
without overwriting any var already set in the real environment, and **fails fast** (exit 1)
naming the first missing required var — the key value is never logged.

Healthy startup logs:

```
... proov.provider: provider online: listening for events
... proov.provider: event received: type=... order_id=... negotiation_id=...
```

**One provider per key.** A second `python -m proov` on the same `CROO_API_KEY` is rejected by
the server with WS code `1008` (policy violation); the watchdog logs *"another provider is
already connected with this key"* and exits non-zero. Reconnect and heartbeat are SDK-managed
(30s ping, 60s pong-timeout, exponential backoff); the adapter keeps the process alive,
surfaces the fatal duplicate-key case, and shuts down gracefully on `SIGINT`/`SIGTERM`.

## Order lifecycle

The provider drives a full CAP transaction end to end:

1. On `order_negotiation_created`, Proov validates the negotiation requirements (see
   [Input validation](#input-validation--graceful-failure)) and, if valid, calls
   `accept_negotiation(negotiation_id)`. The backend's dual-sig `createOrder` fires and both
   parties get `order_created`.
2. On `order_paid`, Proov fetches the order, runs verification, builds a schema-valid
   deliverable, and calls `deliver_order(order_id, DeliverOrderRequest(deliverable_type=SCHEMA,
   deliverable_schema=<json>))`.
3. CAP writes `keccak256(deliverable)` on-chain and settlement releases automatically (price −
   platform fee → Proov's wallet). The order reaches **`completed`**.

Order work is offloaded from the synchronous WS handlers via `asyncio.create_task` and is
**idempotent per id** (a negotiation is accepted at most once, an order delivered at most
once); a handler error is logged and swallowed so the read loop never crashes.

> **Settlement is asynchronous.** `deliver_order` returns with `status=delivering`; the order
> reaches `completed` ~1 min later. Confirm via `get_order().clear_tx_hash`, not by blocking on
> the deliver return. The on-chain fee is 10% (the Order `fee_amount` field is unreliable —
> trust the chain).

## The verification engine

The engine is pure and SDK-agnostic. Both layers below sit behind a `@runtime_checkable`
`Protocol`, so the model or search backend can be swapped by implementing one method and
registering it in a factory — no engine change.

### Claim extraction

Extract discrete, checkable factual claims from an output. `proov/types.py` holds the pure
`Claim`/`Tier` types and per-tier caps; `proov/llm.py` holds the `LLMProvider` interface,
providers, factory, and the `extract_claims` entrypoint.

- **Gemini 2.5 Flash (primary).** A thin raw-REST provider via `httpx` with structured-JSON
  output (no vendor SDK). The key is sent in the `x-goog-api-key` **header, never the `?key=`
  query param**, and is registered as a secret so it can never leak into a log.
- **Caps.** Quick = 20 claims, Deep = default 50; a caller `options.max_claims` may **lower**
  the cap but never raise it above the tier ceiling.
- **Degrade, don't drop.** A transport/status/timeout failure raises `LLMError` (turned into an
  honest `unverifiable` upstream); a successful response that yields no parseable claims returns
  `[]` — an empty extraction is a valid outcome, not a crash.

```
GEMINI_API_KEY=...              # required for live Gemini (free key from Google AI Studio;
                                # GOOGLE_API_KEY is also accepted)
PROOV_LLM_PROVIDER=gemini       # default `gemini`; `stub` for the deterministic offline provider
PROOV_LLM_MODEL=gemini-2.5-flash
PROOV_LLM_TIMEOUT=30            # per-call timeout in seconds (garbage falls back to 30)
```

### Evidence retrieval

Retrieve real, source-linked evidence for each claim. `proov/types.py` gains the `Evidence`
type and per-tier evidence counts; `proov/search.py` holds the `SearchProvider` interface,
providers, factory/chain, and the `retrieve_evidence` entrypoint.

- **Wikipedia (keyless, always-on fallback).** A thin raw-REST provider hitting the MediaWiki
  REST `search/page` endpoint via `httpx`. Match-highlight HTML in each excerpt is stripped to
  a clean snippet.
- **Tavily (optional RAG-native primary).** A thin raw-REST `POST /search` via `httpx`. The key
  is sent in the `Authorization: Bearer` **header, never a URL/body**, and is registered as a
  secret. Free tier ≈ 1,000 searches/mo.
- **Fallback Tavily→Wikipedia, per-claim timeout.** `retrieve_evidence` tries each provider in
  order under a per-call `asyncio.wait_for` timeout; on a provider raising `SearchError` or
  timing out it falls through to the next, and if every provider fails it returns `[]` (the
  claim becomes `unverifiable` downstream). It never raises.
- **`Evidence` is the raw retrieved chunk only** (`source`/`title`/`snippet`/`score?`) — it
  carries no stance; stance is a judgment output.

```
TAVILY_API_KEY=...              # optional RAG-native primary (free key from tavily.com);
                                # without it, retrieval is Wikipedia-only (keyless)
PROOV_SEARCH_PROVIDER=          # force a single `wikipedia|tavily|stub`; unset = auto chain
                                # (Tavily→Wikipedia when keyed, else Wikipedia only)
PROOV_SEARCH_TIMEOUT=10         # per-call (per-claim) timeout in seconds (garbage falls back to 10)
```

### Per-claim judgment

Judge each claim against its retrieved evidence. `proov/types.py` gains the `ClaimStatus`/
`Stance`/`EvidenceStance`/`Judgment` types and the `clamp_confidence` helper; `proov/llm.py`
extends the **same** `LLMProvider` interface with a second method, `judge_claim`, implemented
by both providers, plus the top-level `judge_claim` entrypoint.

- **Same LLM, no new config.** Judgment reuses the *same* pluggable `LLMProvider` (Gemini) and
  the *same* env as extraction (`PROOV_LLM_PROVIDER`, `PROOV_LLM_MODEL`,
  `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `PROOV_LLM_TIMEOUT`) — **no new environment variable**.
  The Gemini judge call sends the key in the `x-goog-api-key` header and asks for a structured
  JSON object.
- **Labels.** Each claim is labelled `supported` / `unsupported` / `unverifiable` with a
  per-claim confidence in `[0,1]` and the supporting/refuting `evidence` (`{source, quote, stance}`).
- **Precision over recall (never a guess).** A judged evidence item is kept only if its `source`
  was actually retrieved (a fabricated source is dropped), and a `supported`/`unsupported` label
  with no surviving grounded evidence is downgraded to `unverifiable`. When evidence is thin —
  or the judge call fails — the claim degrades to `unverifiable` rather than risking a confident
  wrong verdict; one claim's failure never crashes a multi-claim order.

### Citation check (Story 2.4)

When a buyer supplies `sources` with their output, `proov/citations.py` checks each provided
source and flags it `ok` / `fabricated` / `misattributed` for the `citations_checked[]` field.
This is the **provided-sources-only** path (Quick); Deep's "discovered sources" check is
Story 2.7.

- **Two signals, one fetch.** Retrievability is a small injectable `httpx` GET of the source
  URL (status < 400, redirects followed); the fetched, HTML-stripped body doubles as the
  evidence for a support judgment that **reuses the same `LLMProvider`** as extraction and
  per-claim judgment (via `judge_claim`) — **no new LLM config, no new LLM interface**. The
  output is the source's synthetic "attached claim".
- **Flags.** `ok` (retrievable and supports the output, or support merely unconfirmed),
  `fabricated` (NOT retrievable), `misattributed` (retrievable but positively refuted).
- **Precision over recall (never cry wolf).** `fabricated` fires **only** on a confirmed
  unretrievable source (it is the verdict-flipping flag the Story 2.5 `fail` rule keys on),
  and `misattributed` **only** on a positive `unsupported` judgment — mere uncertainty
  (`unverifiable`, or content we couldn't read) is `ok` with support left unconfirmed. The
  check **never crashes a paid order**: a bad source degrades to a conservative non-fabricated
  `ok`, not a false `fabricated`.
- **Config.** One new optional env var, `PROOV_CITATION_TIMEOUT` (seconds, default 10), bounds
  the per-source fetch; support reuses the existing LLM env. No new dependency.

## Input/output contract

The submitted input is the negotiation's `requirements` JSON string:

```json
{ "output": "<required, non-empty string — the AI text to verify>",
  "claims":  ["<optional list of claim strings>"],
  "sources": [{ "url": "https://…", "title": "optional" }],
  "mode":    "quick | deep (advisory — the tier is authoritative from service_id)",
  "options": { "max_claims": 0, "language": "en" } }
```

The delivered output:

```json
{ "verdict": "pass | fail | partial | unverifiable",
  "confidence": 0.0,
  "summary": "string",
  "claims": [],
  "citations_checked": [],
  "stats": {},
  "receipt": { "output_hash": "0x…", "report_hash": "0x…", "verdict": "…",
               "confidence": 0.0, "model": "…", "version": "…", "timestamp": "…",
               "anchor_ref": {} },
  "disclaimer": "string" }
```

## Input validation & graceful failure

Proov never charges a buyer for nothing and never crashes on bad input. A pure validator
(`proov/validation.py` → `validate_requirements`) returns either a normalised input or a
**structured error** with a stable machine code. The byte-size cap is checked on the **raw
string before `json.loads`**, so an oversized payload never materialises a huge object. Unknown
extra keys, an odd `mode`, and extra `options` are **tolerated** (forward-compatible) — Proov
rejects only clearly malformed input, because a wrongly rejected legitimate order costs a real
buyer.

| `code` | meaning |
|--------|---------|
| `output_too_large` | raw `requirements` exceeds the byte cap (default 256 KB, env `PROOV_MAX_INPUT_BYTES`) — checked **before** parse |
| `invalid_json` | not parseable JSON, or not a JSON object |
| `missing_output_field` | no `output` key |
| `output_not_string` | `output` is not a string |
| `empty_output_field` | `output` is blank/whitespace |
| `invalid_sources` | `sources` present but not a list of `{url}` objects |

**Two reject stages (defence in depth):**

1. **Negotiation stage — `reject_negotiation` (buyer never pays).** Malformed input is rejected
   *before* accepting, so no on-chain `createOrder` ever fires and the buyer is never charged.
2. **Paid stage — `reject_order` → auto-refund.** If a paid order's input fails validation, Proov
   calls `reject_order(...)` instead of delivering, and the CAP escrow auto-refunds the
   Requester.

**Graceful degrade.** For a valid paid order, if the verification step raises an internal error,
Proov still delivers a schema-valid deliverable with `verdict: "unverifiable"` (or `"partial"`),
`confidence: 0.0`, an honest summary, and a real populated receipt — so the order reaches
`completed` with value delivered rather than silently timing out.

**SLA-timeout refunds are platform-automatic.** If Proov cannot deliver within an order's SLA,
the CAP escrow automatically refunds the Requester when the deadline passes — no provider
action, and the provider must never manually refund.

## Verifying a receipt

Every delivered verdict carries a tamper-evident, independently re-verifiable on-chain
**receipt**. The deliverable JSON *is* the receipt: its `receipt` object holds real **Ethereum
keccak256** hashes (`output_hash`, `report_hash`), the producing `model`/`version`/`timestamp`,
and an `anchor_ref` descriptor. The whole deliverable is anchored on Base mainnet — the CAP
backend computes `keccak256(deliverable)` and writes it on-chain in the deliver tx, returning it
as `delivery.content_hash`. No custom contract; verification is native.

> **⚠️ keccak256 ≠ SHA3-256.** Base is an Ethereum L2 — the anchor is **Ethereum keccak256**
> (pre-NIST), *not* NIST FIPS-202 SHA3-256. Python's `hashlib.sha3_256` gives a **different**
> digest and will silently fail to match. Use a real keccak (`pycryptodome`: `from Crypto.Hash
> import keccak`). Sanity check: `keccak256("") == 0xc5d2460186f7233c…d85a470` (if you get
> `0xa7ffc6f8bf1ed766…` you have SHA3-256 — the wrong algorithm).

**The byte rule.** The on-chain `content_hash` is `keccak256` of the exact UTF-8 bytes the
provider POSTed. Proov POSTs **canonical JSON** — sorted keys, no whitespace, raw unicode.
`get_delivery(order_id).deliverable_schema` returns a *re-serialised* copy (the backend reorders
keys and decodes unicode escapes), so hashing the returned string verbatim does **not** reproduce
`content_hash`. Re-canonicalise the returned object first:

```python
import json
from Crypto.Hash import keccak

def keccak256_hex(b: bytes) -> str:
    return "0x" + keccak.new(digest_bits=256, data=b).hexdigest()

def canonical_json(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

delivery = await client.get_delivery(order_id)              # deliverable_schema + content_hash
obj = json.loads(delivery.deliverable_schema)
assert keccak256_hex(canonical_json(obj).encode("utf-8")) == delivery.content_hash  # tamper-evident
```

**Step by step:**

1. **Fetch the delivery:** `get_delivery(order_id)` → `deliverable_schema` and `content_hash`.
2. **Tamper-evidence:** `keccak256(canonical_json(json.loads(deliverable_schema))) == content_hash`.
   If anyone altered a byte of the result, this fails.
3. **On-chain confirmation:** confirm `content_hash` is the value written on Base by the order's
   `deliver_tx_hash` (open it on [BaseScan](https://basescan.org)).
4. **Recompute the inner hashes:**
   - `output_hash = keccak256(utf8(<the input output text>))`.
   - `report_hash = keccak256(utf8(canonical_json(deliverable without the receipt and
     verified_by_proov keys)))`. (The receipt can't hash a structure that contains itself.)

> `anchor_ref` is **not** a tx hash. The receipt lives inside the deliverable and `content_hash
> = keccak256(whole deliverable)`. Writing the deliver tx into the receipt would change the very
> bytes being hashed (circular), so `anchor_ref` is a stable descriptor of *where* the anchor
> lives (`chain` / `mechanism` / `anchor_field`).

**Worked example (live on Base mainnet).** Order `cfd6507f-9285-4085-b78c-5efee05d7b7f`, anchor
`content_hash = 0x12cfd3586fb0d2f3864fca95eacedab538377a9fe2c38b80fbdff17e5ba7f89d`, delivered
with a populated receipt and canonical JSON, so all three checks reproduce from `get_delivery`
alone (`.venv/bin/python scripts/probe_anchor.py cfd6507f-9285-4085-b78c-5efee05d7b7f`):
`keccak256(canonical_json(returned)) == content_hash`, `output_hash == keccak256("The Eiffel
Tower is located in Paris, France and was completed in 1889.")`, and `report_hash ==
keccak256(canonical_json(deliverable − receipt))`.

## "Verified by Proov" artifact

The on-chain receipt is reusable currency: any agent that called Proov, and any buyer or auditor
reading a delivered order, gets a portable **"Verified by Proov" artifact** that traces back to
the anchored receipt. A pure builder (`proov/badge.py` → `build_verified_artifact`) derives the
whole payload from a deliverable's `receipt` (plus an optional concrete on-chain `anchor`) — no
new runtime dependency, no I/O.

```json
{ "issuer": "Proov",
  "schema": "proov.verified-by-proov.v1",
  "version": "0.1.0",
  "verdict": "unverifiable", "confidence": 0.0, "model": "stub-no-engine",
  "timestamp": "…Z",
  "output_hash": "0x…", "report_hash": "0x…",
  "anchor_ref": { "chain": "base-mainnet", "mechanism": "cap-deliver-keccak256", "anchor_field": "content_hash" },
  "anchor": null,
  "receipt_id": "0x… (report_hash in-band, content_hash once anchored)",
  "verify": { "rule": "keccak256(canonical_json(json.loads(get_delivery(order_id).deliverable_schema))) == content_hash",
              "procedure": "README#verifying-a-receipt" } }
```

**Two forms:**

1. **In-band badge** — every deliverable carries a top-level `verified_by_proov` object, a
   sibling of `receipt`, with `anchor: null` and `receipt_id = report_hash` (the deliver tx isn't
   known pre-delivery, and embedding it would change the hashed bytes). It is added *after* the
   receipt is computed, so `report_hash` is unchanged — a verifier reproducing `report_hash` must
   strip **both** `receipt` and `verified_by_proov` before re-canonicalising.
2. **Post-delivery, tx-bearing artifact** — assembled after `deliver_order` returns, carrying a
   concrete `anchor = { order_id, content_hash, deliver_tx_hash, delivery_id, chain, explorer_url }`
   and `receipt_id = content_hash`.

**Attaching it to your own delivery.** A caller agent that hired Proov embeds the returned
tx-bearing artifact in *its own* deliverable (e.g. a `proof.verified_by_proov` field), so its
buyer can trace the chain: the caller's deliverable → the embedded artifact →
`anchor.content_hash` / `anchor.deliver_tx_hash` on [Base](https://basescan.org) → the Proov
receipt that re-hashes to it.

## Tests

```bash
pip install -e ".[test]"
pytest
```

The suite runs **fully offline** and needs no API key. Deterministic stub providers and a mocked
`httpx.MockTransport` cover every path — no test opens a real socket or spends LLM/search quota,
and the CAP transaction path runs against a fake SDK client. Live smokes against the real
platform and APIs are kept manual.

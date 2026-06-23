# Proov

**Proof-of-Verification Oracle on the CROO Agent Store.**

A paid, callable CAP agent that verifies the factual claims in an AI-generated output against real sources and returns a verdict, an evidence trail, and a tamper-proof on-chain verification receipt.

- **Network:** Base mainnet (chain 8453) · payments in USDC · gas sponsored by the CROO Paymaster
- **Services:** Quick Check ($0.10, SLA <5 min) · Deep Verify ($0.50, SLA <30 min)
- **Stack:** Python 3.10+ · `croo-sdk` · pluggable LLM/search · SQLite cache

## Status

The CAP transaction path (negotiate → pay → deliver → settle), the tamper-evident on-chain
receipt, input validation and graceful degrade, and the full verification engine —
claim extraction, evidence retrieval, per-claim judgment, citation check and deterministic
verdict aggregation — are built and tested. As of Story 2.6 the **Quick Check** path runs
the whole engine end-to-end: a paid Quick order is verified single-pass and delivered as a
real, schema-valid JSON deliverable whose `verdict`/`confidence`/`claims`/`citations_checked`/
`stats` are the actual aggregated result, with the real model id in a fully populated,
independently verifiable receipt. As of Story 2.7 the **Deep Verify** path is live: it runs
the SAME pipeline keyed on `tier == "deep"` — multi-source evidence merge, multi-pass
(self-consistency) judgment, provided+discovered citations and a 28-min SLA budget — and, for
large reports, also delivers a downloadable full copy via `upload_file` + `get_download_url`
linked from a `report_file` sibling, alongside the same independently verifiable receipt. As of
Story 2.8 a repeated claim is served from a TTL'd SQLite **claim→evidence cache** with no new
search-provider call — the cost/latency enabler of the $0-marginal model at volume. As of Story 3.1
the engine is **calibrated to the ≥80%-precision bar** (NFR4, precision over recall): verdict
precision is measured against a committed ~50-row hand-labeled set and gated offline, and the
`fabricated` citation flag is tightened to fire only on a definitive 404/410 — no more false alarms
on paywalled / rate-limited / transiently-down sources. As of Story 3.2 **order metrics are
instrumented**: the provider mirrors every terminal order into a best-effort SQLite ledger, and
`scripts/dashboard.py` reconciles it with real `list_orders` to surface the success metrics and the
**counter-metrics** (self-trade ratio, cost/order) that prove a win is real.

## How it works

A caller submits an AI-generated `output` (optionally with `claims` and `sources`). Proov:

1. **Extracts** discrete, checkable factual claims from the output (pluggable LLM).
2. **Retrieves** real, source-linked evidence for each claim (pluggable search).
3. **Judges** each claim against its evidence and **aggregates** a deterministic verdict.
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
This is the **provided-sources-only** path (Quick); Deep's "discovered sources" check lands in
Story 2.7 (see **Deep Verify** below) — it appends the engine-surfaced evidence URLs flagged
from their already-assigned stance, at zero extra fetch/judge cost.

- **Two signals, one fetch.** Retrievability is a small injectable `httpx` GET of the source
  URL (status < 400, redirects followed); the fetched, HTML-stripped body doubles as the
  evidence for a support judgment that **reuses the same `LLMProvider`** as extraction and
  per-claim judgment (via `judge_claim`) — **no new LLM config, no new LLM interface**. The
  output is the source's synthetic "attached claim".
- **Flags.** `ok` (retrievable and supports the output, or support merely unconfirmed),
  `fabricated` (a **definitive** 404/410 — the source provably does not exist),
  `misattributed` (retrievable but positively refuted).
- **Precision over recall (never cry wolf).** `fabricated` fires **only** on a confirmed-absent
  source — a definitive 404/410 (it is the verdict-flipping flag the Story 2.5 `fail` rule keys
  on); a restricted / transient response (401/403/429/5xx, a timeout or DNS failure) is
  **ambiguous → `ok`**, never a false `fabricated` (the Story 3.1 precision fix). `misattributed`
  fires **only** on a positive `unsupported` judgment — mere uncertainty (`unverifiable`, or
  content we couldn't read) is `ok` with support left unconfirmed. The check **never crashes a
  paid order**: a bad source degrades to a conservative non-fabricated `ok`.
- **Config.** `PROOV_CITATION_TIMEOUT` (seconds, default 10) bounds the per-source fetch;
  `PROOV_CITATION_USER_AGENT` (Story 3.1) overrides the browser-like fetch User-Agent; support
  reuses the existing LLM env. No new dependency.

### Deterministic verdict (Story 2.5)

`proov/verdict.py` rolls the per-claim judgments (2.3) and per-source citation checks (2.4)
into a single aggregate `Verdict` — one `pass` / `fail` / `partial` label, an overall
confidence, and the PRD §6 `stats` counts `{claims_total, supported, unsupported, unverifiable}`.
Unlike the network slices above, this module is **pure and synchronous** (no `croo`, no `httpx`,
no async, no clock/RNG/env) — its template is `proov/receipt.py`.

- **The FR10 rule** (evaluated in this precedence order):
  - `fail` = ≥1 `fabricated` citation **or** ≥1 `unsupported` (refuted) claim.
  - `pass` = ≥1 claim, **all** `supported`, **no** `fabricated` citation, **no** `unverifiable`
    claim.
  - `partial` = everything else — including zero claims or any `unverifiable` claim (precision
    over recall: `pass` is reserved for a positively-verified output, never asserted on an
    empty or uncertain run).
- **Misattributed citations have no v1 verdict effect** (Open Question 1, literal FR10): only
  `fabricated` gates the label; a `misattributed` source alongside all-`supported` claims still
  yields `pass`. The precision-safe partial-demote alternative is a Story 3.1 calibration question.
- **Deterministic = load-bearing.** The verdict and confidence are hashed into the on-chain
  receipt (Story 2.6 / CAP anchoring), so `aggregate_verdict` is a pure function of its inputs
  (same inputs ⇒ same bytes): the label uses commutative counting and the confidence is computed
  in stable list order.
- **Confidence (v1)** is the mean of the per-claim confidences (a clamped `float`, `0.0` for
  zero claims); the calibrated evidence-agreement + coverage formula is deferred to **Story 3.1**.
- **Wiring** the `Verdict` into the delivered `verdict` / `confidence` / `stats` fields is
  **Story 2.6** (Quick Check end-to-end); this slice only produces the value. No new dependency,
  no new env var.

### Quick Check end-to-end (Story 2.6)

`proov/engine.py` is the SDK-agnostic `[B]` Verification Engine that finally ties the five
slices above together: `verify(input, tier) -> Report` runs the full single-pass pipeline —
**extract claims → (per claim) retrieve evidence + judge → check citations → aggregate
verdict** — and the new `build_deliverable` (`proov/deliverable.py`) maps the resulting
`Report` into a real PRD §6 deliverable + a real receipt. After this story a paid **Quick**
order returns a *real* `pass`/`fail`/`partial` verdict with confidence, a per-claim evidence
trail, citation flags, stats and a tamper-evident on-chain receipt — no longer the stub.

- **Single-pass, sequential.** v1 judges claims one at a time in extraction order (simplest,
  deterministic, and within the free-tier LLM RPM ceiling). Bounded-concurrency / a worker
  pool is **Story 3.3**; the Deep multi-pass tier is **Story 2.7**.
- **Per-order SLA budget → honest early-stop.** A per-order deadline (`PROOV_QUICK_SLA_SECONDS`,
  default 240s — under the 5-min Quick SLA) is checked before each claim; if it is exceeded the
  loop stops early and aggregates whatever was judged into a real **`partial`** (degrade, don't
  drop — NFR3), never a thrown error or an SLA timeout.
- **Real model id (FR14).** The receipt now stamps the active LLM provider's model id
  (`gemini-2.5-flash`, or `stub-llm` offline) instead of the old `stub-no-engine` placeholder —
  the engine resolves the provider once and injects it into both extraction and judgment so the
  stamped model is provably the one that judged.
- **Never raises out.** The engine degrades internally (an extraction failure → zero claims →
  `partial`; the other four slices are already total), so the provider's graceful/reject seam is
  only a belt-and-suspenders backstop. No new dependency; one new env var
  (`PROOV_QUICK_SLA_SECONDS`).

### Deep Verify (Story 2.7)

Deep Verify ($0.50, SLA <30 min) runs the **same** `verify(input, tier)` orchestration as
Quick — extract → (per claim) retrieve + judge → check citations → aggregate → deliverable.
The tier is the only switch; the four Deep differentiators live **inside the slices**, keyed on
`tier == "deep"` (the engine, deliverable builder, receipt and Quick path are untouched):

- **Multi-source evidence merge.** `retrieve_evidence` queries **every** provider in the chain
  (Tavily → Wikipedia) and returns the deduped, capped union (`k = 6`), rather than stopping at
  the first non-empty provider as Quick does. A failed provider contributes nothing rather than
  aborting the merge.
- **Multi-pass (self-consistency) judgment.** The `judge_claim` entrypoint samples the provider
  `PROOV_DEEP_JUDGE_PASSES` times (default 3, capped at 7) per claim and reduces the passes to
  one consensus: **majority status wins; a tie → `unverifiable`** (precision over recall, never
  a coin-flip), and confidence is the agreement-weighted mean of the winning passes (unanimity
  keeps the full mean, a bare majority is penalised). Deterministic and order-independent.
- **Provided + discovered citations.** Beyond the buyer's provided `sources`, the Deep citation
  list **also** covers the **discovered** sources retrieval surfaced — flagged from the stance
  the judge already assigned, at **zero** extra fetch/LLM cost (no re-fetch, no re-judge). A
  discovered source is honest evidence: always `retrievable`, flagged `ok`, never `fabricated`
  (only buyer-provided citations can be `fabricated`/`misattributed`).
- **28-min SLA budget.** `PROOV_DEEP_SLA_SECONDS` (default 1680s) bounds the whole pipeline, with
  the same honest early-stop → `partial` as Quick. (Bounded-concurrency to hit the wall on a
  worst-case 50-claim order is **Story 3.3**; Deep still judges claims sequentially here.)

**Big-report delivery.** The verdict + receipt **always** deliver inline as the anchored,
schema-valid deliverable. When a Deep deliverable's canonical bytes reach
`PROOV_DEEP_UPLOAD_THRESHOLD_BYTES` (default 50 KB), the provider ALSO uploads those exact bytes
via the SDK's `upload_file`, gets a `get_download_url` link, and adds a `report_file`
`{object_key, download_url, size_bytes}` **sibling** (a downloadable full copy). The upload is
best-effort in its own `try/except`: any failure degrades to inline delivery **without**
`report_file` — the file is a convenience, never the verdict, so an upload hiccup never drops a
paid order. `report_file` is added **after** the receipt is computed (like `receipt` /
`verified_by_proov`), so it does **not** change `report_hash` — see "Verifying a receipt".

No new dependency; three new env vars (`PROOV_DEEP_SLA_SECONDS`, `PROOV_DEEP_JUDGE_PASSES`,
`PROOV_DEEP_UPLOAD_THRESHOLD_BYTES`).

### Claim→evidence cache (Story 2.8)

`proov/cache.py` adds a TTL'd, SQLite-backed **claim→evidence cache** `[E]`, wired transparently
into `retrieve_evidence` so the engine and both tiers get it for free. A repeated claim is served
from cache with **zero** search-provider calls — the cost/latency enabler of the $0-marginal model
at volume (FR11).

- **Key = `(normalised claim, tier, k)`.** The claim text is lower-cased and whitespace-collapsed,
  then `sha256`-ed together with the tier and evidence count `k`. Including tier + `k` (not the bare
  claim) means a Quick entry (single source, `k=3`) can never poison a Deep read (the 6-item
  multi-source merge), and a result capped at one `k` is never served for a different `k`.
- **Hit skips search; only non-empty results are cached.** A cache hit returns the stored evidence
  directly. An empty result is **not** cached — caching `[]` would pin a transient search outage as
  "no evidence" for the whole TTL; an empty result is simply re-attempted next time.
- **Best-effort — degrade, don't drop.** Every SQLite/JSON failure degrades to a miss (`get`) /
  no-op (`put`) / `NullCache` (factory). The cache can only make a paid order faster/cheaper; it can
  never fail it or change a verdict (`retrieve_evidence` still never raises out). The hit returns the
  already-normalised list the live path would have produced — the cache changes timing/cost, never
  the data.
- **One lock-guarded connection, off-loaded via `asyncio.to_thread`,** so the always-on WebSocket
  event loop + heartbeat is never blocked on disk I/O, and `:memory:` / file both work.

```
PROOV_CACHE_ENABLED=1            # cache on by default; 0/false/no/off disables (→ NullCache)
PROOV_CACHE_PATH=proov_cache.db  # SQLite file (gitignored via *.db); :memory: also works
PROOV_CACHE_TTL_SECONDS=86400    # entry lifetime in seconds (24 h); garbage falls back to 86400
```

The test suite runs with caching **disabled** (an autouse fixture sets `PROOV_CACHE_ENABLED=0`), so
no `proov_cache.db` is written and existing behaviour is unchanged. No new dependency (`sqlite3` is
stdlib). The order/metrics ledger that will share this `[E]` slot is Story 3.2.

### Calibration to the precision bar (Story 3.1)

Proov's promise is a **trustworthy** verifier: *"a verifier that cries wolf is worse than useless"*
(PRD §1 / NFR4). Story 3.1 makes that measurable, reproducible, and gated — and tightens the one
precision leak the earlier reviews deferred here.

- **The ≥80% precision bar, precision over recall.** `proov/calibration.py` is a **pure**,
  deterministic scorer (built in the style of `proov/verdict.py` — no `croo`, no `httpx`, no I/O in
  the scoring math). It computes a per-class confusion matrix + precision/recall and the **pooled
  precision** over the two verdict-flipping flags (`unsupported` claims, `fabricated` citations).
  The gate (`meets_bar`) keys on **precision only** — recall is reported but never gated: it is
  acceptable to miss a real bad claim, never acceptable to falsely flag a good one. A flag class
  with zero predictions has *undefined* precision (excluded from the gate, never scored `0.0`).
- **A committed hand-labeled set.** `calibration/calibration_set.json` is a ~50-row hand-labeled
  product artifact (≈35 claim rows + ≈15 citation rows) covering the flagged classes and the
  thin-evidence guard. It is **deliberately honest** — it seeds a few real model errors so measured
  precision is below 100% but ≥80%, proving the bar is a genuine threshold, not a tautology.
- **404/410-only `fabricated` (the precision fix).** `proov/citations.py` now classifies
  retrievability three ways: `retrievable` (status < 400), `absent` (a definitive 404/410 → the
  only path to `fabricated`), or `ambiguous` (any other 4xx/5xx, a timeout or DNS failure → the
  conservative `ok`). A paywalled 403, a rate-limited 429, a momentary 503 or a flaky timeout no
  longer produces a false `fail`. Fetches now send a browser-like `User-Agent`
  (`PROOV_CITATION_USER_AGENT` to override).
- **Run it.** `python scripts/calibrate.py` runs the **offline replay**: it feeds the frozen
  recorded model outputs / fetch results through the *real* deterministic pipeline (the grounding
  guards, the new classifier), prints the per-class report and an explicit PASS/FAIL against the
  0.80 bar, and exits non-zero on FAIL — **no network, no spend**, the same path the test suite
  gates on. `python scripts/calibrate.py --live` instead calls real Gemini/Tavily over the dataset
  to **refresh** the frozen snapshot (real spend, requires keys — the operator's empirical run).
- **Gated in the suite ($0, offline).** `tests/test_calibration.py` asserts the pooled, `unsupported`
  and `fabricated` precisions all clear 0.80, that every thin-evidence row resolves to
  `unverifiable` (100%), and that at least one flag class is below 1.0 (so the gate is meaningful).

### Metrics + counter-metric dashboard (Story 3.2)

A win built on concentrated self-trade, or a tier that loses money at its price, is **not** a real
win (PRD §1). So alongside the success metrics, Proov surfaces the **counter-metrics** that catch us
"winning wrong" — and the whole thing reads **real order data**.

- **Two metric kinds.** *Success:* total orders, completed, completion rate, unique buyer wallets
  (and **external** wallets), unique counterparties. *Counter-metrics:* **self-trade ratio**
  (own/companion orders ÷ all — external orders must dominate) and **cost / order** (must stay
  ≈$0 marginal). The third PRD counter-metric, false-fail rate, is the Story 3.1 precision bar —
  not re-done here. Every ratio is **undefined (`n/a`) on a zero denominator**, never a misleading
  `0%` — an empty ledger does not report "0% complete".
- **`list_orders` + a local SQLite ledger, reconciled.** Neither source alone is enough.
  `AgentClient.list_orders()` is the **authoritative** order truth — it sees the async `completed`
  status that lands ~1 min **after** `deliver_order` returns (CLEAR/settlement is server-side, and
  pushed to the *Requester*, so Proov's own delivery-time snapshot is `delivering`, not yet
  `completed`). But the live `Order` has **no tier and no cost** field. So a best-effort SQLite
  **ledger** (`proov/ledger.py`) records, at each terminal order, the facts only Proov knows (tier +
  per-order cost) plus a snapshot for offline use. The dashboard joins them by `order_id`: **live
  status wins**, the **ledger supplies tier + cost**.
- **`proov/metrics.py` is pure; `proov/ledger.py` is best-effort.** The numbers a human acts on are
  computed by a pure, deterministic `compute_metrics` (the `proov/verdict.py` / `proov/calibration.py`
  template — no `croo`, no I/O, same inputs ⇒ same numbers). The ledger touches disk inside the
  always-on event loop, so it mirrors the cache's discipline: one lock-guarded connection,
  `asyncio.to_thread`-offloaded, every failure degrades to a no-op. The provider's record hook is
  **double-guarded** and runs only **after** an order is already terminal — it can never slow or
  fail a paid order (NFR3).
- **Run it.** `python scripts/dashboard.py` prints the dashboard **offline** from the local ledger —
  **$0, no keys, no network** (the ledger's snapshot status, honest except the `delivering→completed`
  lag). `python scripts/dashboard.py --live` instead reads real `list_orders` (provider role) and
  reconciles it with the ledger (live status wins; ledger supplies tier/cost) — operator-only, needs
  keys, never run by the test suite.
- **Self-trade config.** `PROOV_OWN_AGENT_IDS` (comma-separated) marks Proov's own/companion agent
  ids; until the companion Research caller's id is minted in Epic 4.2 it is empty and the self-trade
  ratio is honestly `n/a`/`0%`. **$0 cost stance:** `cost / order` is the documented free-tier `0.0`
  today (overridable via `PROOV_QUICK_COST_USD` / `PROOV_DEEP_COST_USD`); a measured per-order cost
  and a ceiling are Story 3.4 — this dashboard makes the $0-marginal claim **visible and falsifiable**
  now.

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

A large **Deep** deliverable additionally carries an optional `report_file`
`{ "object_key": "…", "download_url": "https://…", "size_bytes": 0 }` — a link to a downloadable
full copy of the canonical deliverable. It is a post-receipt sibling (like `verified_by_proov`),
so it is excluded when reproducing `report_hash`.

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
   - `report_hash = keccak256(utf8(canonical_json(deliverable without the receipt,
     verified_by_proov, and report_file keys)))`. (The receipt can't hash a structure that
     contains itself; `report_file` — present only on large Deep deliverables — is a
     post-receipt sibling, so it is stripped too.)

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
  "verdict": "pass", "confidence": 0.0, "model": "gemini-2.5-flash",
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
   strip `receipt` and `verified_by_proov` (and, on a large Deep deliverable, the `report_file`
   sibling) before re-canonicalising.
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

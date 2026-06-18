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
CROO_SDK_KEY=croo_sk_...
```

Copy `.env.example` to `.env` and fill in your `croo_sk_...` API key (issued once in the Agent Store dashboard). **Never commit `.env`.**

# argo-x402-reference

**A self-hosted x402 paywall + facilitator for selling LLM inference over HTTP —
running live on Base mainnet, paid in USDC, on a homelab GPU.**

This repo is the reference implementation behind a live endpoint. Any human or
AI agent with a crypto wallet can buy a completion in one HTTP round-trip:
no accounts, no API keys, no signup. Payment verifies on-chain *before* the
model runs; settlement happens *after* — a failed request is never charged.

```
$0.01  POST /premium/llm    27B open-weights model, 4K token cap
$0.05  POST /premium/deep   8K tokens, smart-routes to a frontier model when warranted
$0.10  POST /premium/vl     30B vision model, image understanding
free   GET  /premium/balance  live on-chain earnings (transparency endpoint)
free   POST /premium/trial    3 calls per wallet, sybil-limited by EIP-191 signature
```

## Why

x402 (HTTP 402 "Payment Required", revived by Coinbase) lets software pay
software. This repo is the smallest serious stack we found that does it
**self-hosted end to end**:

- `x402_facilitator.py` — your own facilitator (verify + settle). No hosted
  service, no middleman cut, no regional blocks.
- `x402_paywall.py` — FastAPI middleware: 402 with payment requirements →
  buyer signs → facilitator verifies → handler runs → settlement receipt in
  the response body (`X-PAYMENT-RESPONSE`).
- `x402_mcp_server.py` — MCP wrapper so Claude/desktop agents can purchase natively.
- `scripts/x402_client_demo.py` — a working buyer in ~90 lines.

## Architecture

```
buyer agent ──POST /premium/llm──▶ Caddy (path allowlist) ──▶ FastAPI paywall
                                   ▲                            │
                                   │ 402 + payment requirements │ verify via
                                   │                            ▼ facilitator
                              wallet signs ──────────────▶ on-chain (Base)
                                                                │
                                            handler runs AFTER verify
                                            settles AFTER success (≥400 cancels)
```

Security posture (the interesting part for homelab operators):

- The public internet reaches **only** the paywall routes. A reverse-proxy
  path allowlist 404s everything else — the rest of the machine (fleet
  control, admin API, this repo's private origin) is unreachable by design.
- Wallet keys are age-encrypted at rest, decrypted in memory only.
- Prompts are not logged or retained. The response dies with the connection.
- Token caps per tier bound worst-case compute cost per call; every call is
  prepaid, so abuse is self-funding (it pays us).

## Run it

```bash
pip install fastapi uvicorn x402 c web3
# receive + facilitator wallets: age-encrypted JSON, decrypted at startup
age-encrypt your wallet json → vault/x402_receive_wallet.age
uvicorn app:app --port 8900   # app wires the middleware onto your routes
```

Point a buyer at it:

```bash
python3 scripts/x402_client_demo.py "$URL/premium/llm" "Write a haiku about payments"
```

## Status

- Live on Base mainnet (chain 8453), USDC settlement.
- First real external settlement: 0.12 USDC and counting — see the balance
  endpoint on the live deployment.
- Hardware: one RTX 5090 running an AWQ-quantized 27B. ~70% gross margin per
  $0.01 call after electricity.

## Honest limits

This is homelab infra, not an SLA product: best-effort uptime, one GPU,
no data-retention paperwork — because there is no data retention.
Positioning is exactly that: **best-effort, cheap, keyless, and honest.**

License: MIT.

#!/usr/bin/env python3
"""
x402 client demo — pay $0.01 (test USDC) for a premium hub completion.

Usage:
  X402_PAYER_KEY=0x... python3 scripts/x402_client_demo.py "Explain x402 in one sentence"

The payer wallet needs test USDC on Base Sepolia (plus a little ETH for gas).
Faucets: https://faucet.circle.com (USDC) / Coinbase dev faucet (ETH).
NEVER use a production key here. Get one fresh from any wallet (MetaMask etc).

Flow exercised:
  1. POST /premium/llm                       -> 402 PaymentRequired (b64 JSON header)
  2. Sign EIP-3009 USDC authorization (v2)   -> retry with X-PAYMENT header
  3. Hub verifies via facilitator, runs LLM, settles on-chain
  4. Response carries X-PAYMENT-RESPONSE (settlement receipt, b64 JSON)
"""
import base64
import json
import os
import sys

import httpx

HUB_URL = os.environ.get("X402_HUB_URL", "http://localhost:8900")
PREMIUM_PATH = "/premium/llm"


def b64json(s: str):
    return json.loads(base64.b64decode(s))


def main():
    key = os.environ.get("X402_PAYER_KEY")
    if not key:
        sys.exit("Set X402_PAYER_KEY=<0x... evm private key with Base Sepolia test USDC>")
    prompt = " ".join(sys.argv[1:]) or "Say hello in exactly five words."

    from eth_account import Account
    from x402 import x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    acct = Account.from_key(key)
    print(f"payer:   {acct.address}")
    print(f"target:  {HUB_URL}{PREMIUM_PATH}")

    client = x402ClientSync()
    register_exact_evm_client(client, acct)  # eip155:* wildcard (v2)
    http = x402HTTPClientSync(client)

    body = {"prompt": prompt, "max_tokens": 500}
    with httpx.Client(timeout=180) as hc:
        # Step 1 — get the challenge
        r1 = hc.post(f"{HUB_URL}{PREMIUM_PATH}", json=body)
        if r1.status_code != 402:
            sys.exit(f"expected 402, got {r1.status_code}: {r1.text[:200]}")

        req_hdr = r1.headers.get("payment-required", "")
        pr = b64json(req_hdr)
        accepts = pr.get("accepts", [{}])[0]
        print(f"challenge: v{pr.get('x402Version')} {accepts.get('scheme')} "
              f"{accepts.get('network')} amount={accepts.get('amount')} "
              f"({int(accepts.get('amount', 0))/1e6:.2f} USDC) -> {accepts.get('payTo')}")

        # Step 2 — sign and build retry headers
        payment_headers, _payload = http.handle_402_response(
            headers=dict(r1.headers), body=r1.content, request_url=str(r1.request.url),
        )
        print("signed:  EIP-3009 authorization ready")

        # Step 3 — retry with payment
        r2 = hc.post(f"{HUB_URL}{PREMIUM_PATH}", json=body, headers=payment_headers)
        print(f"result:  HTTP {r2.status_code}")

        settle_hdr = r2.headers.get("x-payment-response", "")
        if r2.status_code == 200:
            data = r2.json()
            print(f"\n=== PREMIUM RESPONSE ===\n{data.get('text', '')[:800]}")
            print(f"\nmodel_tier={data.get('model_tier')} elapsed={data.get('elapsed_s')}s")
            if settle_hdr:
                sr = b64json(settle_hdr)
                print(f"\n=== SETTLEMENT ===\nsuccess: {sr.get('success')}")
                print(f"tx:      https://sepolia.basescan.org/tx/{sr.get('transaction')}")
                print(f"payer:   {sr.get('payer')}")
        else:
            print("body:", r2.text[:400])
            if settle_hdr:
                print("settlement:", b64json(settle_hdr))
            sys.exit(1)


if __name__ == "__main__":
    main()

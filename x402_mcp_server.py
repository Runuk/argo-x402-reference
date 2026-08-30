#!/usr/bin/env python3
"""
Argo Hub x402 MCP Server
========================
Exposes the hub's paid premium endpoints as MCP tools. Any MCP client
(Claude Desktop, Hermes, etc.) can purchase inference natively — payment is
x402 over HTTP (USDC on Base mainnet), signed locally from the payer key in
the vault. Never sends the key anywhere; only signatures leave the machine.

Tools:
  - premium_llm(prompt, max_tokens?)            $0.01  Qwen3.8-27B
  - premium_deep(prompt, max_tokens?)           $0.05  8K cap + frontier routing
  - premium_vl(prompt, image_url, max_tokens?)  $0.10  Qwen3-VL 30B
  - premium_balance()                           free   on-chain earnings

Free trial: first 3 calls per wallet are free. Pass use_trial=True once to
sign a trial ticket; the wrapper tracks remaining calls and stops offering
it when exhausted.

Config: ARGO_HUB_URL (default http://localhost:8900).
Stdio transport: run with `mcp-remote http://...` NOT required — this is a
plain stdio MCP server. Register in the MCP client config as a command.
"""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx

HUB_URL = os.environ.get("ARGO_HUB_URL", "http://localhost:8900").rstrip("/")
VAULT_PAYER = Path("$HOME/argo-hub/vault/x402_payer_wallet.age")
AGE_IDENTITY = Path("~/.age/key.txt")

TRIAL_DOMAIN = "argo-hub x402 free trial v1"
_trial_state = {"remaining": None}  # None = unknown yet


def _load_payer_account():
    import subprocess
    from eth_account import Account

    p = subprocess.run(
        ["age", "-d", "-i", str(AGE_IDENTITY), str(VAULT_PAYER)],
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"vault decrypt failed: {p.stderr.decode()[:200]}")
    data = json.loads(p.stdout)
    acct = Account.from_key(data["private_key"])
    return acct


_ACCOUNT = None


def get_account():
    global _ACCOUNT
    if _ACCOUNT is None:
        _ACCOUNT = _load_payer_account()
    return _ACCOUNT


async def _pay_and_post(path: str, body: dict, tier_price_usd: str) -> tuple[int, dict]:
    """Full x402 client flow: POST → 402 challenge → sign → retry → parse."""
    import time

    from eth_account.messages import encode_defunct
    from x402.client import x402Client as _XClient
    from x402.http import x402HTTPClient
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    account = get_account()
    client = _XClient()
    register_exact_evm_client(
        client,
        EthAccountSigner(account),
        networks="eip155:8453",
    )
    http_client = x402HTTPClient(client)

    async def _flow():
        async with httpx.AsyncClient(timeout=180) as hx:
            # 1) initial POST — expect 402 challenge
            r = await hx.post(f"{HUB_URL}{path}", json=body)
            if r.status_code != 402:
                return r.status_code, r.json() if r.content else {}

            # 2) free-trial attempt: dedicated unprotected route carries
            #    trial_auth (the paid routes 402-challenge headerless calls
            #    before any handler runs, so trials live on /premium/trial)
            if _trial_state["remaining"] is None or _trial_state["remaining"] > 0:
                msg = f"{TRIAL_DOMAIN}|{path}|{account.address}"
                sig = account.sign_message(encode_defunct(text=msg)).signature
                trial_body = {
                    **body,
                    "tier": path.rsplit("/", 1)[-1],
                    "trial_auth": {
                        "wallet": account.address,
                        "message": msg,
                        "signature": "0x" + sig.hex(),
                    },
                }
                tr = await hx.post(f"{HUB_URL}/premium/trial", json=trial_body)
                if tr.status_code == 200:
                    data = tr.json()
                    if data.get("trial"):
                        _trial_state["remaining"] = data.get("free_calls_remaining", 0)
                        return 200, data

            # 3) paid flow: build payment from the 402 challenge and retry
            payment_required = http_client.get_payment_required_response(
                lambda name: r.headers.get(name), r.content
            )
            payload = await http_client.create_payment_payload(payment_required)
            headers = http_client.encode_payment_signature_header(payload)
            r2 = await hx.post(f"{HUB_URL}{path}", json=body, headers=headers)
            data = r2.json() if r2.content else {}
            try:
                result = await http_client.process_payment_result(
                    payload, lambda name: r2.headers.get(name), r2.status_code
                )
                settle = getattr(result, "settlement", None)
                if settle is not None and getattr(settle, "success", False):
                    tx = getattr(settle, "transaction", None)
                    if tx:
                        data.setdefault("settlement", {})
                        data["settlement"] = {
                            "tx": tx,
                            "payer": getattr(settle, "payer", None),
                            "network": getattr(settle, "network", None),
                            "amount_atomic": getattr(settle, "amount", None),
                            "explorer": f"https://basescan.org/tx/{tx}",
                        }
            except Exception:
                pass
            return r2.status_code, data

    return await _flow()


def _fmt(status: int, data: dict) -> str:
    out = {"status": status}
    if status == 200:
        out.update({k: data.get(k) for k in (
            "text", "model_tier", "elapsed_s", "model", "usage",
            "trial", "free_calls_remaining", "settlement",
        ) if k in data})
    else:
        out["error"] = data.get("error") or data
    return json.dumps(out, indent=2)


async def _amain():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("argo-x402", instructions=(
        "Paid inference from the Argo fleet via x402 micropayments "
        "(USDC on Base mainnet). Payment is automatic per call; "
        "premium_balance shows the wallet and earnings."
    ))

    @mcp.tool()
    async def premium_llm(prompt: str, max_tokens: int = 1500,
                          use_trial: bool = True) -> str:
        """$0.01 — completion on Qwen3.8-27B (32K ctx). Pays with the fleet wallet."""
        body = {"prompt": prompt, "max_tokens": max_tokens}
        if not use_trial:
            _trial_state["remaining"] = 0
        status, data = await _pay_and_post("/premium/llm", body, "0.01")
        return _fmt(status, data)

    @mcp.tool()
    async def premium_deep(prompt: str, max_tokens: int = 4000,
                           use_trial: bool = True) -> str:
        """$0.05 — 8K token cap + smart routing to frontier models when warranted."""
        body = {"prompt": prompt, "max_tokens": max_tokens}
        if not use_trial:
            _trial_state["remaining"] = 0
        status, data = await _pay_and_post("/premium/deep", body, "0.05")
        return _fmt(status, data)

    @mcp.tool()
    async def premium_vl(prompt: str, image_url: str,
                         max_tokens: int = 2000) -> str:
        """$0.10 — Qwen3-VL 30B analyzes an image (public URL or data: URL)."""
        body = {"prompt": prompt, "image_url": image_url, "max_tokens": max_tokens}
        status, data = await _pay_and_post("/premium/vl", body, "0.10")
        return _fmt(status, data)

    @mcp.tool()
    async def premium_balance() -> str:
        """Free — the hub's receive wallet, network, price list, on-chain earnings."""
        async with httpx.AsyncClient(timeout=30) as hx:
            r = await hx.get(f"{HUB_URL}/premium/balance")
            status, data = r.status_code, r.json()
        return json.dumps({"status": status, **data}, indent=2)

    return mcp


if __name__ == "__main__":
    server = asyncio.run(_amain())
    server.run(transport="stdio")


#!/usr/bin/env python3
"""
Self-hosted x402 facilitator for Argo Hub — MAINNET capable.
============================================================
Serves the x402 facilitator API on port 8402:
  GET  /supported              -> schemes/networks this facilitator settles
  POST /verify                 -> verify a payer's signed authorization (read-only)
  POST /settle                 -> broadcast the USDC transfer on-chain

Why self-host: the hosted facilitator (x402.org) is EVM-testnet-only.
This one runs the same protocol against Base mainnet (chain 0x2105).

Security model:
  - The facilitator wallet (vault/x402_facilitator_wallet.age) ONLY pays gas.
    Customer USDC flows payer -> receive wallet directly (EIP-3009
    transferWithAuthorization). The facilitator cannot move customer funds.
  - Private key is decrypted in-memory at startup, never written to disk/env.

Run:  systemctl --user enable --now x402-facilitator
"""
import inspect
import json
import logging
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("x402-facilitator")

BASE_DIR = Path(__file__).resolve().parent
VAULT_FACILITATOR = BASE_DIR / "vault" / "x402_facilitator_wallet.age"
AGE_IDENTITY = Path("~/.age/key.txt")

CHAIN_RPC = "https://mainnet.base.org"      # Base mainnet
NETWORK = "eip155:8453"                     # Base mainnet CAIP-2

app = FastAPI(title="Argo x402 Facilitator", version="1.0")

_facilitator = None


def _decrypt_key() -> str:
    p = subprocess.run(
        ["age", "-d", "-i", str(AGE_IDENTITY), str(VAULT_FACILITATOR)],
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"vault decrypt failed: {p.stderr.decode()[:200]}")
    return json.loads(p.stdout)["private_key"]


def get_facilitator():
    """Lazily build the x402Facilitator with an EVM signer on Base mainnet."""
    global _facilitator
    if _facilitator is not None:
        return _facilitator

    from x402 import x402Facilitator
    from x402.mechanisms.evm.exact.register import register_exact_evm_facilitator
    from x402.mechanisms.evm.signers import FacilitatorWeb3Signer

    pk = _decrypt_key()
    signer = FacilitatorWeb3Signer(private_key=pk, rpc_url=CHAIN_RPC)

    facilitator = x402Facilitator()
    register_exact_evm_facilitator(facilitator, signer, NETWORK)
    _facilitator = facilitator
    log.info("facilitator ready: signer=%s network=%s", signer.address, NETWORK)
    return _facilitator


@app.get("/supported")
async def supported():
    f = get_facilitator()
    return f.get_supported()


@app.post("/verify")
async def verify(request: Request):
    from x402 import parse_payment_payload
    from x402.schemas import PaymentRequirements, PaymentRequirementsV1
    f = get_facilitator()
    body = await request.json()
    try:
        payload = parse_payment_payload(body.get("paymentPayload"))
        raw_reqs = body.get("paymentRequirements")
        if getattr(payload, "x402_version", 2) == 1:
            reqs = PaymentRequirementsV1.model_validate(raw_reqs)
        else:
            reqs = PaymentRequirements.model_validate(raw_reqs)
        result = f.verify(payload, reqs)
        if inspect.iscoroutine(result):
            result = await result
        return JSONResponse(json.loads(result.model_dump_json()))
    except Exception as e:
        log.exception("verify failed")
        return JSONResponse({"isValid": False, "invalidReason": str(e)}, status_code=400)


@app.post("/settle")
async def settle(request: Request):
    from x402 import parse_payment_payload
    from x402.schemas import PaymentRequirements, PaymentRequirementsV1
    f = get_facilitator()
    body = await request.json()
    try:
        payload = parse_payment_payload(body.get("paymentPayload"))
        raw_reqs = body.get("paymentRequirements")
        if getattr(payload, "x402_version", 2) == 1:
            reqs = PaymentRequirementsV1.model_validate(raw_reqs)
        else:
            reqs = PaymentRequirements.model_validate(raw_reqs)
        result = f.settle(payload, reqs)
        if inspect.iscoroutine(result):
            result = await result
        return JSONResponse(json.loads(result.model_dump_json()))
    except Exception as e:
        log.exception("settle failed")
        return JSONResponse({"success": False, "errorReason": str(e)}, status_code=400)


@app.get("/health")
async def health():
    return {"ok": True, "network": NETWORK, "rpc": CHAIN_RPC}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8402)

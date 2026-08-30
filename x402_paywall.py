"""
x402 Paywall — premium endpoints on the Argo Hub.
=================================================
First proof of x402 (HTTP-402 micropayments) on the fleet.

Design:
  - x402 FastAPI middleware protects ONE route:
      POST /premium/llm  →  llm_complete_hub with premium treatment
  - Network: Base mainnet (eip155:8453), asset: USDC, price: $0.01/call
  - Settlement: hosted facilitator (https://x402.org/facilitator)
    verifies + settles on-chain; hub never holds buyer keys/funds
  - Receive wallet: age-encrypted at vault/x402_receive_wallet.age
  - Middleware short-circuits unless the path matches a protected route —
    the rest of the hub (fleet tools, dashboard) is untouched.

Flow (standard x402):
  1. Client POSTs without X-PAYMENT header → 402 + PaymentRequired challenge
  2. Client signs EIP-3009 transferWithAuthorization (USDC on Base)
  3. Client retries with X-PAYMENT header
  4. Middleware verifies via facilitator → runs handler → settles on-chain
  5. Success response carries X-PAYMENT-RESPONSE header (settlement receipt)
  Note: settlement happens AFTER the handler. Handler errors (>=400) cancel
  settlement, so callers are never charged for a failed completion.
"""
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("x402_paywall")

BASE_DIR = Path(__file__).resolve().parent
VAULT_WALLET = BASE_DIR / "vault" / "x402_receive_wallet.age"
AGE_IDENTITY = Path("~/.age/key.txt")

# MAINNET (eip155:8453) via SELF-HOSTED facilitator (x402_facilitator.py, :8402).
# Hosted x402.org is EVM-testnet-only. Testnet fallback: NETWORK="eip155:84532",
# FACILITATOR_URL="https://x402.org/facilitator".
NETWORK = "eip155:8453"            # Base mainnet
PRICE_USD = "0.01"                 # standard completion
PRICE_DEEP_USD = "0.05"            # deep: more tokens + smart routing
PRICE_VL_USD = "0.10"              # vision-language (Qwen3-VL 30B swap)
FACILITATOR_URL = "http://127.0.0.1:8402"
PREMIUM_ROUTE = "/premium/llm"
PREMIUM_DEEP_ROUTE = "/premium/deep"
PREMIUM_VL_ROUTE = "/premium/vl"
PREMIUM_BALANCE_ROUTE = "/premium/balance"   # free — earnings readout
PREMIUM_MAX_TOKENS_CAP = 4000
PREMIUM_DEEP_TOKENS_CAP = 8000
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base mainnet USDC

# ── Free trial (sybil-limited) ─────────────────────────────────────────
# First N calls free per wallet. Identity = EIP-191 signature over a fixed
# domain string; the signing key IS the wallet (sybil cost = new key = new
# on-chain identity, which is exactly the "chain id sybil window" from the
# GTM plan). Trial state is in-memory only — a hub restart resets counts,
# which is fine: this is a marketing lever, not an accounting system.
import base64 as _b64
import hashlib as _hashlib

FREE_TRIAL_CALLS = 3
TRIAL_DOMAIN = "argo-hub x402 free trial v1"
# Trial ledger persists in hub.db (survives hub restarts; restarts must NOT
# hand every wallet a fresh 3 free calls). Table: x402_trials.

def _trial_db():
    import sqlite3
    conn = sqlite3.connect(str(BASE_DIR / "hub.db"))
    conn.execute("""CREATE TABLE IF NOT EXISTS x402_trials (
        wallet TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0,
        first_ts REAL, last_ts REAL)""")
    return conn

def _trial_get(conn, wallet: str) -> int:
    row = conn.execute("SELECT used FROM x402_trials WHERE wallet=?", (wallet,)).fetchone()
    return row[0] if row else 0

def _recover_trial_signer(message: str, signature_hex: str) -> str | None:
    """Recover the signer address from an EIP-191 personal_sign signature."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        sig = signature_hex.strip()
        if sig.startswith("0x"):
            sig = sig[2:]
        if len(sig) != 130:
            return None
        r = int(sig[0:64], 16)
        s = int(sig[64:128], 16)
        v = int(sig[128:130], 16)
        if v not in (27, 28):
            v += 27  # normalize 0/1-style v from some wallets/libraries
        who = Account.recover_message(
            encode_defunct(text=message), vrs=(v, r, s)
        )
        return who.lower()
    except Exception:
        return None

def check_free_trial(
    request_body: dict | None,
    x_payment_header: str | None,
) -> tuple[bool, Any]:
    """Attempt to admit the call under the free trial.

    Returns (admitted, response) — if admitted is True the handler must skip
    the paid path and return `response` (a JSON-serializable dict wrapped by
    the caller into a JSONResponse) directly.
    """
    from fastapi.responses import JSONResponse

    if x_payment_header:
        # Client came ready to pay — never intercept a paying caller.
        return False, None

    auth = ((request_body or {}).get("trial_auth") or {})
    wallet = str(auth.get("wallet") or "").strip().lower()
    signature = str(auth.get("signature") or "").strip()
    nonce = str(auth.get("message") or "").strip()
    if not (wallet.startswith("0x") and len(wallet) == 42 and signature and nonce):
        return False, None  # not a trial attempt — fall through to paid path

    if TRIAL_DOMAIN not in nonce:
        return False, JSONResponse({
            "error": "trial_auth.message must contain the domain string",
            "domain": TRIAL_DOMAIN,
        }, status_code=400)

    who = _recover_trial_signer(nonce, signature)
    if who is None or who != wallet:
        return False, JSONResponse({
            "error": "trial_auth signature invalid or not from claimed wallet",
        }, status_code=401)

    conn = _trial_db()
    ts = time.time()
    used = _trial_get(conn, who)
    if used >= FREE_TRIAL_CALLS:
        conn.close()
        return False, JSONResponse({
            "error": "free trial exhausted for this wallet",
            "wallet": who,
            "free_calls_used": used,
            "free_calls_allowed": FREE_TRIAL_CALLS,
            "next_step": "send the request with an X-PAYMENT header (x402) — see /premium/balance for pricing",
        }, status_code=402)

    used += 1
    conn.execute(
        """INSERT INTO x402_trials (wallet, used, first_ts, last_ts) VALUES (?,?,?,?)
           ON CONFLICT(wallet) DO UPDATE SET used=?, last_ts=?""",
        (who, used, ts, ts, used, ts),
    )
    conn.commit()
    conn.close()
    return True, {
        "trial": True,
        "wallet": who,
        "free_calls_used": used,
        "free_calls_remaining": FREE_TRIAL_CALLS - used,
    }

_address_cache: dict[str, Any] = {"addr": None, "ts": 0.0}


def get_receive_address() -> str:
    """Decrypt the receive wallet from the vault; cache address in memory only."""
    if _address_cache["addr"] and time.time() - _address_cache["ts"] < 3600:
        return _address_cache["addr"]
    p = subprocess.run(
        ["age", "-d", "-i", str(AGE_IDENTITY), str(VAULT_WALLET)],
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"vault decrypt failed: {p.stderr.decode()[:200]}")
    addr = json.loads(p.stdout)["address"]
    _address_cache["addr"] = addr
    _address_cache["ts"] = time.time()
    return addr


def build_premium_middleware():
    """Build the x402 FastAPI payment middleware for premium routes.

    Returns None when x402 or the vault is unavailable — the hub then runs
    unpaywalled (paywall inactive, everything else unaffected).
    """
    try:
        from x402.http import HTTPFacilitatorClient
        from x402.http.middleware.fastapi import payment_middleware
        from x402.mechanisms.evm.exact.register import register_exact_evm_server
        from x402.server import x402ResourceServer

        pay_to = get_receive_address()
    except Exception as e:
        log.warning("x402 paywall disabled (fail-open): %s", e)
        return None

    facilitator = HTTPFacilitatorClient({"url": FACILITATOR_URL})
    resource_server = x402ResourceServer(facilitator)
    # Server-side 'exact' scheme: parses "$0.01" → USDC amount, builds EIP-712 domain
    register_exact_evm_server(resource_server, NETWORK)

    routes = {
        f"POST {PREMIUM_ROUTE}": _accept(pay_to, PRICE_USD, {
            "description": "Premium LLM completion — Qwen3.8-27B on private GPU fleet",
            "mimeType": "application/json",
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {"prompt": "Write a haiku about payments"},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "max_tokens": {"type": "integer"},
                        "department": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
            },
            "output": {"type": "application/json", "example": {"text": "...", "model_tier": "premium", "elapsed_s": 1.7}},
        }),
        f"POST {PREMIUM_DEEP_ROUTE}": _accept(pay_to, PRICE_DEEP_USD, {
            "description": "Deep tier — 8K tokens, smart routing to frontier models when warranted",
            "mimeType": "application/json",
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {"prompt": "Explain zero-knowledge proofs"},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "max_tokens": {"type": "integer"},
                        "department": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
            },
            "output": {"type": "application/json", "example": {"text": "...", "model_tier": "deep", "routing": "auto_frontier"}},
        }),
        f"POST {PREMIUM_VL_ROUTE}": _accept(pay_to, PRICE_VL_USD, {
            "description": "Vision tier — Qwen3-VL 30B analyzes an image URL",
            "mimeType": "application/json",
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {"prompt": "What is in this image?", "image_url": "https://example.com/photo.jpg"},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "image_url": {"type": "string", "format": "uri"},
                        "max_tokens": {"type": "integer"},
                    },
                    "required": ["prompt", "image_url"],
                },
            },
            "output": {"type": "application/json", "example": {"text": "...", "model_tier": "vl"}},
        }),
    }

    log.info(
        "x402 paywall active: POST %s @ $%s, POST %s @ $%s, POST %s @ $%s → %s",
        PREMIUM_ROUTE, PRICE_USD, PREMIUM_DEEP_ROUTE, PRICE_DEEP_USD,
        PREMIUM_VL_ROUTE, PRICE_VL_USD, pay_to,
    )
    return payment_middleware(routes, resource_server, sync_facilitator_on_start=True)


def _accept(pay_to: str, price: str, discovery: dict | None = None) -> dict:
    acc = {
        "scheme": "exact",
        "payTo": pay_to,
        "price": price,
        "network": NETWORK,
    }
    ext: dict[str, Any] = {}
    if discovery:
        # x402 Bazaar discovery extension — makes the route queryable in
        # facilitator/discovery directories so agents can find it.
        # Required shape: {"info": {...}, "schema": <JSON Schema that the
        # *info itself* must satisfy>}. The SDK jsonschema-validates info
        # against this schema, so it must describe info's shape (not the
        # API payload's).
        ext["bazaar"] = {
            "info": discovery,
            "schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "mimeType": {"type": "string"},
                    "input": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "method": {"type": "string"},
                            "bodyType": {"type": "string"},
                            "body": {"type": "object"},
                            "input_schema": {"type": "object"},
                        },
                    },
                    "output": {"type": "object"},
                },
                "required": ["description", "mimeType", "input"],
            },
        }
        return {"accepts": acc, "extensions": ext}
    return {"accepts": acc}


# ── The protected handler ──────────────────────────────────────────────
async def premium_llm_handler(request):
    """$0.01 tier: standard completion on Qwen3.8-27B.

    Delegates to the shared pipeline (trial gate + completion + JSON body).
    """
    return await _run_completion(
        request, tier="premium", default_model="local",
        tokens_cap=PREMIUM_MAX_TOKENS_CAP, default_tokens=1500,
    )


# ── Shared completion plumbing ─────────────────────────────────────────
async def _complete(prompt: str, department: str, max_tokens: int,
                    tier: str, default_model: str) -> dict:
    """Shared completion core — returns a plain dict, no HTTP wrapper.

    On failure returns {"error": ..., "status_code": int}.
    """
    from server import llm_complete_hub

    t0 = time.time()
    try:
        result: Any = await llm_complete_hub(
            prompt, department, max_tokens=max_tokens, temperature=0.7,
            agent=f"x402-{tier}", model=default_model,
        )
    except Exception as e:
        log.exception("%s completion failed", tier)
        return {"error": f"completion failed: {e}", "status_code": 502}

    if isinstance(result, dict) and result.get("error") and "choices" not in result:
        return {"error": result["error"], "status_code": 502}

    if isinstance(result, dict) and "choices" in result:
        msg = (result.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content")
        if not (text or "").strip():
            text = (msg.get("reasoning") or msg.get("reasoning_content") or "")[-1500:]
        meta = {
            "model": result.get("model"),
            "routing": result.get("llm_routing"),
            "usage": result.get("usage"),
        }
    else:
        text, meta = str(result), {}

    return {"text": text or "", "elapsed_s": round(time.time() - t0, 2), **meta}


def decode_settlement_receipt(header_value: str | None) -> dict | None:
    """Decode X-PAYMENT-RESPONSE (b64 SettleResponse JSON) into a body-friendly dict."""
    import base64
    raw = (header_value or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(_b64.b64decode(raw + "=" * (-len(raw) % 4)))
        if isinstance(data, dict) and data.get("success"):
            tx = data.get("transaction")
            return {
                "tx": tx,
                "payer": data.get("payer"),
                "network": data.get("network"),
                "amount_atomic": data.get("amount"),
                "explorer": f"https://basescan.org/tx/{tx}" if tx else None,
            }
    except Exception:
        return None
    return None


async def _run_completion(request, *, tier: str, default_model: str,
                          tokens_cap: int, default_tokens: int):
    """Common body-parse + llm_complete_hub call + text extraction for paid tiers."""
    from fastapi.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    try:
        max_tokens = min(int(body.get("max_tokens", default_tokens)), tokens_cap)
    except (TypeError, ValueError):
        max_tokens = default_tokens
    # SECURITY: external payers get a fixed allowlist of departments. Never
    # accept arbitrary strings — they become persona/system-prompt material in
    # llm_complete_hub and could shape (not leak, but shape) generation.
    _ALLOWED_DEPTS = {"saas", "web_dev", "creative_writing", "mathematics", "blockchain"}
    requested = body.get("department") or "saas"
    department = requested if requested in _ALLOWED_DEPTS else "saas"

    # ── Free trial gate (x402: first N calls free per wallet) ──
    from fastapi.responses import JSONResponse as _JR
    admitted, trial_resp = check_free_trial(
        body, request.headers.get("x-payment")
    )
    if admitted:
        import hmac as _hmac
        import hashlib as _hash
        result = await _complete(prompt, department, max_tokens, tier, default_model)
        if isinstance(result, dict) and result.get("status_code"):
            return _JR({"error": result["error"]}, status_code=result["status_code"])
        proof = _hmac.new(
            b"argo-trial", f"{trial_resp['wallet']}-{trial_resp['free_calls_used']}".encode(), _hash.sha256
        ).hexdigest()[:12]
        return _JR({
            **trial_resp,
            "text": result["text"],
            "model_tier": tier,
            "department": department,
            "max_tokens": max_tokens,
            "elapsed_s": result["elapsed_s"],
            "model": result["model"],
            "usage": result["usage"],
            "trial_proof": proof,
            "paid_network": NETWORK,
            "note": f"free trial call {trial_resp['free_calls_used']}/{FREE_TRIAL_CALLS} — send X-PAYMENT header when ready to pay",
        })
    if trial_resp is not None:
        return trial_resp  # 400/401/402 trial rejection
    # ── paid path ──
    result = await _complete(prompt, department, max_tokens, tier, default_model)
    if result.get("status_code"):
        return JSONResponse({"error": result["error"]}, status_code=result["status_code"])
    return JSONResponse({
        "text": result["text"],
        "model_tier": tier,
        "department": department,
        "max_tokens": max_tokens,
        "elapsed_s": result["elapsed_s"],
        "paid_network": NETWORK,
    })


async def deep_llm_handler(request):
    """$0.05 tier: 8K token cap + model='auto' (smart local/frontier routing)."""
    return await _run_completion(
        request, tier="deep", default_model="auto",
        tokens_cap=PREMIUM_DEEP_TOKENS_CAP, default_tokens=4000,
    )


async def trial_handler(request):
    """POST /premium/trial — free-tier entry (NOT behind the payment middleware).

    The x402 middleware 402-challenges headerless requests before any handler
    runs, so trials need their own unprotected route; the EIP-191 wallet gate
    in check_free_trial IS the protection here (3 calls per wallet, tier
    pooled). Body: {prompt, tier?: llm|deep|vl, image_url?, max_tokens?, trial_auth{...}}
    """
    from fastapi.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"prompt": "prompt required", "error": "prompt required"}, status_code=400)

    tier = body.get("tier") or "llm"
    if tier not in ("llm", "deep", "vl"):
        tier = "llm"
    if tier == "vl" and not (body.get("image_url") or "").strip():
        return JSONResponse({"error": "image_url required for tier=vl"}, status_code=400)

    _TIER_CFG = {
        "llm": ("local", PREMIUM_MAX_TOKENS_CAP, 1500),
        "deep": ("auto", PREMIUM_DEEP_TOKENS_CAP, 2000),
        "vl": ("vl", PREMIUM_DEEP_TOKENS_CAP, 1500),
    }
    default_model, tokens_cap, default_tokens = _TIER_CFG[tier]
    try:
        max_tokens = min(int(body.get("max_tokens", default_tokens)), tokens_cap)
    except (TypeError, ValueError):
        max_tokens = default_tokens
    _ALLOWED_DEPTS = {"saas", "web_dev", "creative_writing", "mathematics", "blockchain"}
    requested = body.get("department") or "saas"
    department = requested if requested in _ALLOWED_DEPTS else "saas"

    admitted, trial_resp = check_free_trial(body, None)
    if not admitted:
        return trial_resp  # None-auth rejection (400/401/402) — never None here

    full_prompt = f"{prompt}\n\n[image: {body.get('image_url')}]" if tier == "vl" else prompt
    import hmac as _hmac
    import hashlib as _hash
    result = await _complete(full_prompt, department, max_tokens, f"trial-{tier}", default_model)
    if result.get("status_code"):
        return JSONResponse({"error": result["error"]}, status_code=result["status_code"])
    proof = _hmac.new(
        b"argo-trial", f"{trial_resp['wallet']}-{trial_resp['free_calls_used']}".encode(), _hash.sha256
    ).hexdigest()[:12]
    out = {
        **trial_resp,
        "text": result["text"],
        "model_tier": tier,
        "department": department,
        "max_tokens": max_tokens,
        "elapsed_s": result["elapsed_s"],
        "trial_proof": proof,
        "paid_network": NETWORK,
        "note": f"free trial call {trial_resp['free_calls_used']}/{FREE_TRIAL_CALLS} — paid tiers: POST /premium/{tier} with x402 X-PAYMENT",
    }
    if result.get("model"):
        out["model"] = result["model"]
    if result.get("usage"):
        out["usage"] = result["usage"]
    return JSONResponse(out)


async def vl_llm_handler(request):
    """$0.10 tier: Qwen3-VL 30B vision-language swap. Accepts image_url in body."""
    from fastapi.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    prompt = (body.get("prompt") or "").strip()
    image_url = (body.get("image_url") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)
    if not image_url:
        return JSONResponse(
            {"error": "image_url required for VL tier (public URL or data: URL)"},
            status_code=400,
        )

    full_prompt = f"{prompt}\n\n[image: {image_url}]"
    from server import llm_complete_hub

    t0 = time.time()
    try:
        result = await llm_complete_hub(
            full_prompt, body.get("department") or "web_dev",
            max_tokens=min(int(body.get("max_tokens", 2000)), PREMIUM_DEEP_TOKENS_CAP),
            temperature=0.7, agent="x402-vl", model="vl",
        )
    except Exception as e:
        log.exception("vl completion failed")
        return JSONResponse({"error": f"completion failed: {e}"}, status_code=502)

    if isinstance(result, dict) and result.get("error") and "choices" not in result:
        return JSONResponse({"error": result["error"]}, status_code=502)

    msg = (result.get("choices") or [{}])[0].get("message") or {} if isinstance(result, dict) else {}
    text = msg.get("content") or ""
    if not text.strip():
        text = (msg.get("reasoning") or msg.get("reasoning_content") or "")[-1500:]

    return JSONResponse({
        "text": text,
        "model_tier": "vl",
        "model": result.get("model") if isinstance(result, dict) else None,
        "image_analyzed": image_url[:200],
        "elapsed_s": round(time.time() - t0, 2),
        "paid_network": NETWORK,
        "note": "VL tier triggers 30B vision model swap; first call includes ~60s swap time",
    })


# ── Free endpoint: on-chain earnings readout ──────────────────────────
_rpc_cache: dict[str, Any] = {"bal": None, "ts": 0.0}


async def balance_handler(request):
    """GET/POST /premium/balance — live earnings (receive wallet USDC on-chain)."""
    from fastapi.responses import JSONResponse
    import httpx

    if _rpc_cache["bal"] is None or time.time() - _rpc_cache["ts"] > 30:
        try:
            import httpx as _hx
            addr = get_receive_address()
            data = "0x70a08231000000000000000000000000" + addr[2:].lower()
            async with _hx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    "https://mainnet.base.org",
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                          "params": [{"to": USDC_MAINNET, "data": data}, "latest"]},
                )
                raw = r.json().get("result", "0x0")
            _rpc_cache["bal"] = int(raw, 16) / 1e6
            _rpc_cache["ts"] = time.time()
        except Exception as e:
            log.warning("balance lookup failed: %s", e)
            if _rpc_cache["bal"] is None:
                return JSONResponse({"error": "balance lookup failed"}, status_code=502)

    return JSONResponse({
        "earnings_usdc": _rpc_cache["bal"],
        "wallet": _address_cache.get("addr"),
        "network": NETWORK,
        "price_list": {
            "POST /premium/llm": PRICE_USD,
            "POST /premium/deep": PRICE_DEEP_USD,
            "POST /premium/vl": PRICE_VL_USD,
        },
        "cached_seconds_ago": round(time.time() - _rpc_cache["ts"], 1),
    })

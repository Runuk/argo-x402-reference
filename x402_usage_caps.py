"""Per-wallet usage caps for the x402 premium API.

OWNER POLICY (2026-08-30): a buyer cannot extract unbounded GPU-hours for
pennies. Two independent limits, enforced per wallet per rolling UTC day:

  1. CALLS cap  — max paid calls per wallet per day      (default 100)
  2. TOKENS cap — max total max_tokens per wallet per day (default 400K)

Both are enforced AFTER payment verification but BEFORE model execution —
an over-cap call returns 429 with a retry hint and the caller is never
charged (settlement only happens on handler success).

Wallet identity: the payer address inside the X-PAYMENT payload (recovered
from the EIP-712 authorization before settlement). Table: x402_usage.
Raise limits in one place: CAPS below.
"""
import json, sqlite3, time
from pathlib import Path

HUB = Path(".")
DB = HUB / "hub.db"

# ── The one place to tune ────────────────────────────────────────────
DAILY_CALLS_CAP = 100          # paid calls per wallet per UTC day
DAILY_TOKENS_CAP = 400_000  # summed max_tokens per wallet per UTC day
# 100 calls ≈ $1.00-5.00/day/wallet spend ceiling depending on tier mix.
# A data-miner wanting more must spread across wallets = on-chain visible = fine.

def _conn():
    c = sqlite3.connect(str(DB), timeout=20)
    c.execute("""CREATE TABLE IF NOT EXISTS x402_usage (
        wallet TEXT NOT NULL,
        day TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (wallet, day))""")
    return c

def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())

def payer_from_header(header_value: str | None) -> str | None:
    """Extract payer address from the base64 X-PAYMENT payload (pre-verify)."""
    if not header_value:
        return None
    import base64
    try:
        raw = base64.b64decode(header_value)
        obj = json.loads(raw)
        # x402 payload shapes: payload.authorization.from / payload.from
        auth = (obj.get("payload") or {}).get("authorization") or {}
        frm = (auth.get("from") or (obj.get("payload") or {}).get("from") or "").lower()
        if frm.startswith("0x") and len(frm) == 42:
            return frm
    except Exception:
        pass
    return None

def check_and_count(wallet: str | None, tokens: int) -> tuple[bool, dict | None]:
    """Check caps and count this call. Returns (allowed, rejection_response_dict).

    wallet=None → cannot attribute (shouldn't happen post-middleware); allow
    but don't count, so a parsing bug never takes the store offline.
    """
    if not wallet:
        return True, None
    day = _today()
    with _conn() as c:
        row = c.execute("SELECT calls, tokens FROM x402_usage WHERE wallet=? AND day=?",
                        (wallet, day)).fetchone()
        calls_used = row[0] if row else 0
        tokens_used = row[1] if row else 0
        if calls_used >= DAILY_CALLS_CAP or tokens_used + tokens > DAILY_TOKENS_CAP:
            return False, {
                "error": "daily usage cap reached for this wallet",
                "caps": {"calls_per_day": DAILY_CALLS_CAP, "tokens_per_day": DAILY_TOKENS_CAP},
                "used_today": {"calls": calls_used, "tokens": tokens_used},
                "resets_at": f"{day}T23:59:59Z (UTC)",
                "note": "you were not charged for this request",
            }
        c.execute("""INSERT INTO x402_usage (wallet, day, calls, tokens) VALUES (?,?,1,?)
                     ON CONFLICT(wallet, day) DO UPDATE SET calls=calls+1, tokens=tokens+?""",
                  (wallet, day, tokens, tokens))
    return True, None

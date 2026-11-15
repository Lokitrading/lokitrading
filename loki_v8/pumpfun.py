"""pump.fun, read straight from its Solana program: event decoding and exact bonding-curve maths.

pump.fun has no public trading API (its website API sits behind Cloudflare), but every launch and
every trade on its bonding curves is an event its program writes to the Solana log. This module reads
those events, so the connector is pump.fun's own program rather than a third party's copy of it.

* ``events_from_logs`` pulls the ``CreateEvent``, ``TradeEvent`` and ``CompleteEvent`` records the
  pump.fun program emitted in one transaction (only while pump.fun itself was executing, so a lookalike
  event from another program is never read).
* Standard launches are priced in SOL and start at 30 virtual SOL against 1,073,000,000 virtual
  tokens. Launches priced in other tokens are newer variants and are skipped (``is_standard``).
* ``buy_tokens`` / ``sell_sol`` are the curve's constant-product maths with its fees, so a simulated
  fill is the fill the program would give at those reserves.

Paper only: nothing here signs or sends a transaction.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any, Dict, List

PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LAMPORTS = 1_000_000_000          # per SOL
TOKEN_UNIT = 1_000_000            # pump.fun tokens have 6 decimals
START_VSOL = 30 * LAMPORTS        # virtual SOL reserve at launch
START_VTOKENS = 1_073_000_000 * TOKEN_UNIT
START_RTOKENS = 793_100_000 * TOKEN_UNIT  # tokens sold on the curve before it graduates
DEFAULT_FEE_BPS = 125             # 0.95% protocol + 0.30% creator, as the program charged in Nov 2026
SYSTEM_PROGRAM = "11111111111111111111111111111111"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


TRADE, CREATE, COMPLETE = _disc("TradeEvent"), _disc("CreateEvent"), _disc("CompleteEvent")


def b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def _string(raw: bytes, o: int) -> "tuple[str, int]":
    n = struct.unpack_from("<I", raw, o)[0]
    if n > 400 or o + 4 + n > len(raw):
        raise ValueError("bad string length")
    return raw[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n


def parse_trade(raw: bytes) -> Dict[str, Any]:
    """A bonding-curve trade. Amounts stay in raw units (lamports, 6-decimal token units)."""
    o = 8
    mint = b58(raw[o:o + 32]); o += 32
    sol, tokens = struct.unpack_from("<QQ", raw, o); o += 16
    is_buy = raw[o] == 1; o += 1
    user = b58(raw[o:o + 32]); o += 32
    ts, vsol, vtok, rsol, rtok = struct.unpack_from("<qQQQQ", raw, o); o += 40
    o += 32  # fee recipient
    fee_bps = struct.unpack_from("<Q", raw, o)[0]; o += 16
    creator = b58(raw[o:o + 32]); o += 32
    creator_bps = struct.unpack_from("<Q", raw, o)[0]
    return {"mint": mint, "sol": sol, "tokens": tokens, "buy": is_buy, "user": user, "ts": ts,
            "vsol": vsol, "vtok": vtok, "rsol": rsol, "rtok": rtok, "fee_bps": fee_bps + creator_bps,
            "creator": creator}


def parse_create(raw: bytes) -> Dict[str, Any]:
    o = 8
    name, o = _string(raw, o)
    symbol, o = _string(raw, o)
    _uri, o = _string(raw, o)
    mint = b58(raw[o:o + 32]); o += 32
    curve = b58(raw[o:o + 32]); o += 32
    o += 32  # the signer
    creator = b58(raw[o:o + 32]); o += 32
    ts, vtok, vsol, rtok, supply = struct.unpack_from("<qQQQQ", raw, o); o += 40
    # Newer launches append the token program, the Mayhem-mode flag and the quote mint (native SOL is
    # the system id). In Mayhem mode an automated agent trades the launch without fees for a while.
    mayhem = len(raw) > o + 32 and raw[o + 32] == 1
    quote = b58(raw[o + 33:o + 65]) if len(raw) >= o + 65 else SYSTEM_PROGRAM
    return {"mint": mint, "curve": curve, "creator": creator, "name": name[:40], "symbol": symbol[:16],
            "ts": ts, "vsol": vsol, "vtok": vtok, "rtok": rtok, "supply": supply, "quote": quote, "mayhem": mayhem}


def parse_complete(raw: bytes) -> Dict[str, Any]:
    o = 8 + 32
    mint = b58(raw[o:o + 32]); o += 64
    return {"mint": mint, "ts": struct.unpack_from("<q", raw, o)[0]}


def is_standard(create: Dict[str, Any]) -> bool:
    """A launch priced in SOL on the standard curve, the only kind the bot trades."""
    return bool(create.get("vsol") == START_VSOL and create.get("vtok") == START_VTOKENS
                and create.get("rtok") == START_RTOKENS and create.get("quote") == SYSTEM_PROGRAM)


def events_from_logs(logs: List[str]) -> List[Dict[str, Any]]:
    """pump.fun events in one successful transaction's logs, in order, each tagged with its ``kind``."""
    stack: List[str] = []
    out: List[Dict[str, Any]] = []
    for line in logs:
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split()[1])
        elif line.startswith("Program ") and (line.endswith(" success") or " failed" in line):
            if stack:
                stack.pop()
        elif line.startswith("Program data: ") and stack and stack[-1] == PROGRAM:
            try:
                raw = base64.b64decode(line[14:])
                head = raw[:8]
                if head == TRADE:
                    out.append({"kind": "trade", **parse_trade(raw)})
                elif head == CREATE:
                    out.append({"kind": "create", **parse_create(raw)})
                elif head == COMPLETE:
                    out.append({"kind": "complete", **parse_complete(raw)})
            except (ValueError, struct.error):
                continue  # a layout this decoder does not know: skip it rather than guess
    return out


# --- bonding-curve maths ---------------------------------------------------------------------------
def price(vsol: float, vtok: float) -> float:
    """SOL per whole token."""
    return (vsol / LAMPORTS) / (vtok / TOKEN_UNIT) if vtok > 0 else 0.0


def buy_tokens(vsol: int, vtok: int, rtok: int, lamports_in: int, fee_bps: int = DEFAULT_FEE_BPS) -> int:
    """Tokens a buy of ``lamports_in`` (fees included) receives at these reserves."""
    spend = lamports_in * 10_000 // (10_000 + fee_bps)
    out = vtok - (vsol * vtok) // (vsol + spend) - 1
    return max(0, min(out, rtok))


def sell_sol(vsol: int, vtok: int, tokens_in: int, fee_bps: int = DEFAULT_FEE_BPS) -> int:
    """Lamports a sale of ``tokens_in`` receives at these reserves, after fees."""
    gross = vsol - (vsol * vtok) // (vtok + tokens_in)
    return max(0, gross - gross * fee_bps // 10_000)


def progress(rtok: int) -> float:
    """Share of the curve sold, 0 to 1; at 1 the token graduates to pump.fun's AMM."""
    return max(0.0, min(1.0, 1 - rtok / START_RTOKENS))


def market_cap_sol(vsol: int, vtok: int, supply: int = 1_000_000_000 * TOKEN_UNIT) -> float:
    return price(vsol, vtok) * supply / TOKEN_UNIT


# --- bonding-curve accounts -------------------------------------------------------------------------
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def is_mint(text: str) -> bool:
    """A base58 Solana address (32 bytes)."""
    try:
        return 32 <= len(text) <= 44 and len(b58decode(text)) == 32
    except ValueError:
        return False


def _on_curve(key: bytes) -> bool:
    """Whether 32 bytes are a valid ed25519 point (program addresses must not be)."""
    y = int.from_bytes(key, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def bonding_curve_address(mint: str) -> str:
    """The pump.fun bonding-curve account of a mint (its program-derived address)."""
    seeds = b"bonding-curve" + b58decode(mint)
    program = b58decode(PROGRAM)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(seeds + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58(h)
    raise ValueError("no program address")


def parse_curve_account(data: bytes) -> Dict[str, Any]:
    """The bonding-curve account: reserves, supply, whether it graduated, and its creator."""
    vtok, vsol, rtok, rsol, supply = struct.unpack_from("<QQQQQ", data, 8)
    complete = data[48] == 1
    creator = b58(data[49:81]) if len(data) >= 81 else ""
    mayhem = len(data) > 81 and data[81] == 1
    return {"vtok": vtok, "vsol": vsol, "rtok": rtok, "rsol": rsol, "supply": supply, "complete": complete,
            "creator": creator, "mayhem": mayhem, "price_sol": price(vsol, vtok), "progress": progress(rtok) if not complete else 1.0,
            "market_cap_sol": market_cap_sol(vsol, vtok, supply), "raised_sol": rsol / LAMPORTS}

"""pump.fun connector: the trained bot's record, a relay of pump.fun's live program events, and a
live read of any launch's bonding curve.

* ``/pumpfun``: the tested record and the rule the browser bot runs.
* ``/pumpfun/stream``: for up to 50 seconds per request, the server subscribes to the pump.fun
  program's logs on a Solana node, decodes them (``loki_v8/pumpfun.py``) and sends the browser one
  compact JSON line per launch, trade and graduation, plus a tick each second. Solana's public node
  refuses connections from web pages, and the raw stream is about 0.6 MB a second; the relay sends
  roughly 10 KB a second. The browser bot runs on these rows' timestamps, so a late batch never
  changes a paper fill.
* ``/pumpfun/curve``: one token's bonding-curve account and Jupiter's live market for it.
* ``/pumpfun/quote``: Jupiter's executable quote for a buy or sell, the second connector. The browser
  bot checks every paper fill against it.

Paper only; nothing here signs or sends a transaction.
"""
from __future__ import annotations

import asyncio
import base64
import json
import ssl
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from loki.api.fusion import _private_response
from loki_v8 import pumpfun as pf

router = APIRouter(prefix="/api/fusion/ai", tags=["Loki Fusion"])

RECORD_PATH = Path(__file__).resolve().parents[2] / "loki_v8" / "pumpfun_record.json"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
CURVE_SECONDS = 10.0
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_lock = threading.Lock()


def record() -> Dict[str, Any]:
    try:
        loaded: Dict[str, Any] = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
        return loaded
    except (OSError, ValueError):
        return {}


def _rpc_urls() -> List[str]:
    extra = [u.strip() for u in os.environ.get("SOLANA_RPC_URLS", "").split(",") if u.strip().startswith("https://")]
    return extra + [PUBLIC_RPC]


def _rpc(method: str, params: List[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = "no endpoint answered"
    for url in _rpc_urls():
        try:
            req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "LokiPumpConnector/1.0"})
            with urllib.request.urlopen(req, timeout=8) as res:  # noqa: S310 - fixed https endpoints
                out = json.loads(res.read().decode())
            if "error" in out:
                last = str(out["error"].get("message", "error"))[:120]
                continue
            return out.get("result")
        except (OSError, ValueError, urllib.error.URLError) as error:
            last = type(error).__name__
    raise HTTPException(503, f"Solana could not be read right now ({last}); try again shortly.")


def bonding_curve(mint: str) -> Optional[Dict[str, Any]]:
    """The pump.fun bonding-curve account of a mint, or None if pump.fun has no curve for it."""
    address = pf.bonding_curve_address(mint)
    result = _rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
    value = (result or {}).get("value")
    if not value or value.get("owner") != pf.PROGRAM:
        return None
    state = pf.parse_curve_account(base64.b64decode(value["data"][0]))
    return {
        "bonding_curve": address, "graduated": state["complete"],
        "progress_pct": round(state["progress"] * 100, 2), "price_sol": state["price_sol"],
        "market_cap_sol": round(state["market_cap_sol"], 3), "raised_sol": round(state["raised_sol"], 4),
        "creator": state["creator"], "mayhem": state["mayhem"],
        "standard_curve": state["supply"] == 1_000_000_000 * pf.TOKEN_UNIT and not state["mayhem"],
        "solscan": f"https://solscan.io/account/{address}",
    }


# --- Jupiter: Solana's main trade router, the second connector ---------------------------------------
# It routes pump.fun's bonding curves ("Pump.fun") and its AMM after graduation ("Pump.fun Amm"), so it
# gives a real executable price for any pump.fun token, at any stage. Free public tier, no key.
JUPITER = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_SECONDS = 3.0


def _jupiter(path: str) -> Any:
    """GET a Jupiter endpoint; None when Jupiter has no answer for it (no route, unknown token)."""
    req = urllib.request.Request(JUPITER + path, headers={"Accept": "application/json", "User-Agent": "LokiPumpConnector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as res:  # noqa: S310 - fixed https endpoint
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as error:
        if error.code in (400, 404):
            return None
        raise HTTPException(503, f"Jupiter answered {error.code}; try again shortly.") from error
    except (OSError, ValueError) as error:
        raise HTTPException(503, f"Jupiter could not be reached ({type(error).__name__}); try again shortly.") from error


def jupiter_quote(mint: str, side: str, amount: int) -> Optional[Dict[str, Any]]:
    """Jupiter's executable quote: buy ``amount`` lamports of the token, or sell ``amount`` raw token units."""
    src, dst = (SOL_MINT, mint) if side == "buy" else (mint, SOL_MINT)
    q = _jupiter(f"/swap/v1/quote?inputMint={src}&outputMint={dst}&amount={int(amount)}&slippageBps=1500")
    if not isinstance(q, dict) or "outAmount" not in q:
        return None
    return {"side": side, "in_amount": int(q.get("inAmount", amount)), "out_amount": int(q["outAmount"]),
            "price_impact_pct": round(float(q.get("priceImpactPct") or 0) * 100, 3),
            "route": [str((leg.get("swapInfo") or {}).get("label", "?")) for leg in q.get("routePlan") or []],
            "quoted_at": round(time.time(), 3)}


def jupiter_price(mint: str) -> Optional[Dict[str, Any]]:
    data = _jupiter(f"/price/v3?ids={mint}")
    row = (data or {}).get(mint) if isinstance(data, dict) else None
    if not isinstance(row, dict) or row.get("usdPrice") is None:
        return None
    return {"price_usd": row.get("usdPrice"), "liquidity_usd": row.get("liquidity"),
            "change_24h_pct": row.get("priceChange24h"), "launchpad": row.get("launchpad")}


def token(mint: str) -> Dict[str, Any]:
    """pump.fun's curve (if any) and Jupiter's live market for a token, side by side."""
    if not pf.is_mint(mint):
        raise HTTPException(422, "That is not a Solana token address.")
    curve_state = bonding_curve(mint)
    market: Optional[Dict[str, Any]] = None
    try:
        market = jupiter_price(mint)
        if market is not None:
            market["buy_0_1_sol"] = jupiter_quote(mint, "buy", 100_000_000)
    except HTTPException:
        market = None  # the curve alone still answers
    if curve_state is None and market is None:
        raise HTTPException(404, "Neither pump.fun nor Jupiter knows that token.")
    return {
        "mint": mint, "curve": curve_state, "jupiter": market,
        "links": {"pump_fun": f"https://pump.fun/coin/{mint}", "solscan": f"https://solscan.io/token/{mint}"},
        "read_at": int(time.time()),
        "note": "The curve is read from the pump.fun program's account on Solana; the market from Jupiter, which "
                "routes pump.fun's curve before graduation and its AMM after.",
    }


PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
MAX_STREAMS = 12
_streams = 0


async def relay(seconds: float) -> AsyncIterator[str]:
    """NDJSON rows of pump.fun launches, trades and graduations, then an end row."""
    from loki_v8.pumpfun_bot import rows_from_logs
    import websockets

    global _streams
    _streams += 1
    try:
        url = os.environ.get("SOLANA_WS_URL", PUBLIC_WS)
        ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
        sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe", "params": [{"mentions": [pf.PROGRAM]}, {"commitment": "confirmed"}]}
        deadline = time.time() + seconds
        async with websockets.connect(url, ssl=ctx, max_size=2 ** 24, open_timeout=10, ping_interval=None) as ws:
            await ws.send(json.dumps(sub))
            yield json.dumps({"k": "open", "at": round(time.time(), 3)}) + "\n"
            last_tick = 0.0
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, min(1.0, deadline - time.time())))
                except asyncio.TimeoutError:
                    raw = None
                out: List[str] = []
                if raw is not None:
                    msg = json.loads(raw)
                    res = (msg.get("params") or {}).get("result")
                    if res and not res["value"].get("err"):
                        out = [json.dumps(r, separators=(",", ":")) for r in rows_from_logs(res["context"]["slot"], res["value"].get("logs") or [], time.time())]
                now = time.time()
                if now - last_tick >= 1.0:
                    last_tick = now
                    out.append(json.dumps({"k": "tick", "at": round(now, 3)}))
                if out:
                    yield "\n".join(out) + "\n"
        yield json.dumps({"k": "end", "at": round(time.time(), 3)}) + "\n"
    except (OSError, asyncio.TimeoutError, ValueError, Exception) as error:  # noqa: BLE001 - report, never hang
        yield json.dumps({"k": "error", "detail": f"Solana's stream could not be read ({type(error).__name__})."}) + "\n"
    finally:
        _streams -= 1


@router.get("/pumpfun/stream")
async def ai_pumpfun_stream(seconds: float = Query(25.0, ge=5.0, le=50.0)) -> StreamingResponse:
    """Live pump.fun launches and trades, decoded from the program's logs, as NDJSON rows. Read-only."""
    if _streams >= MAX_STREAMS:
        raise HTTPException(503, "The live pump.fun relay is busy; try again in a minute.")
    return StreamingResponse(relay(seconds), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/pumpfun")
def ai_pumpfun(response: Response) -> Dict[str, Any]:
    """The pump.fun bot: its tested record, the rule the browser runs, and the program's event layout."""
    _private_response(response)
    rec = record()
    if not rec:
        raise HTTPException(503, "The pump.fun bot has no tested record on this server yet.")
    return {
        **rec,
        "program": pf.PROGRAM,
        "events": {"trade": pf.TRADE.hex(), "create": pf.CREATE.hex(), "complete": pf.COMPLETE.hex()},
        "curve": {"start_vsol": pf.START_VSOL, "start_vtokens": pf.START_VTOKENS, "start_rtokens": pf.START_RTOKENS,
                  "fee_bps": pf.DEFAULT_FEE_BPS},
        "stream": "/api/fusion/ai/pumpfun/stream",
    }


@router.get("/pumpfun/quote")
def ai_pumpfun_quote(response: Response, mint: str = Query(..., min_length=32, max_length=44),
                     side: str = Query(..., pattern="^(buy|sell)$"),
                     amount: int = Query(..., gt=0, le=10 ** 18)) -> Dict[str, Any]:
    """Jupiter's executable quote for buying (lamports in) or selling (raw token units in) a token. Read-only."""
    _private_response(response)
    if not pf.is_mint(mint):
        raise HTTPException(422, "That is not a Solana token address.")
    key = f"q:{mint}:{side}:{amount}"
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
    if cached and now - cached[0] < QUOTE_SECONDS:
        return cached[1]
    quote = jupiter_quote(mint, side, amount)
    if quote is None:  # an ordinary answer for a token too new or too thin to route, not a failure
        quote = {"side": side, "available": False, "reason": "Jupiter has no route for that trade right now."}
    else:
        quote["available"] = True
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (now, quote)
    return quote


@router.get("/pumpfun/curve")
def ai_pumpfun_curve(response: Response, mint: str = Query(..., min_length=32, max_length=44)) -> Dict[str, Any]:
    """One token: its pump.fun bonding curve (if any), read from Solana, and Jupiter's live market for it."""
    _private_response(response)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(mint)
    if cached and now - cached[0] < CURVE_SECONDS:
        return cached[1]
    out = token(mint)
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[mint] = (now, out)
    return out

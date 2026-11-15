"""pump.fun connector: the trained bot's record, a relay of pump.fun's live program events, and a
live read of any launch's bonding curve.

* ``/pumpfun``: the tested record and the rule the browser bot runs.
* ``/pumpfun/stream``: for up to 50 seconds per request, the server subscribes to the pump.fun
  program's logs on a Solana node, decodes them (``loki_v8/pumpfun.py``) and sends the browser one
  compact JSON line per launch, trade and graduation, plus a tick each second. Solana's public node
  refuses connections from web pages, and the raw stream is about 0.6 MB a second; the relay sends
  roughly 10 KB a second. The browser bot runs on these rows' timestamps, so a late batch never
  changes a paper fill.
* ``/pumpfun/curve``: one launch's bonding-curve account.

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
from typing import Any, AsyncIterator, Dict, List, Tuple

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


def curve(mint: str) -> Dict[str, Any]:
    if not pf.is_mint(mint):
        raise HTTPException(422, "That is not a Solana token address.")
    address = pf.bonding_curve_address(mint)
    result = _rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
    value = (result or {}).get("value")
    if not value:
        raise HTTPException(404, "No pump.fun bonding curve exists for that token.")
    if value.get("owner") != pf.PROGRAM:
        raise HTTPException(404, "That account does not belong to pump.fun.")
    data = base64.b64decode(value["data"][0])
    state = pf.parse_curve_account(data)
    standard = state["supply"] == 1_000_000_000 * pf.TOKEN_UNIT and not state["mayhem"]
    return {
        "mint": mint, "bonding_curve": address, "graduated": state["complete"],
        "progress_pct": round(state["progress"] * 100, 2), "price_sol": state["price_sol"],
        "market_cap_sol": round(state["market_cap_sol"], 3), "raised_sol": round(state["raised_sol"], 4),
        "creator": state["creator"], "mayhem": state["mayhem"], "standard_curve": standard,
        "links": {"pump_fun": f"https://pump.fun/coin/{mint}", "solscan": f"https://solscan.io/account/{address}"},
        "read_at": int(time.time()),
        "note": "Read straight from the pump.fun program's account on Solana. A graduated token now trades on "
                "pump.fun's AMM, not on this curve.",
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


@router.get("/pumpfun/curve")
def ai_pumpfun_curve(response: Response, mint: str = Query(..., min_length=32, max_length=44)) -> Dict[str, Any]:
    """Live state of one pump.fun launch's bonding curve, read from Solana."""
    _private_response(response)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(mint)
    if cached and now - cached[0] < CURVE_SECONDS:
        return cached[1]
    out = curve(mint)
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[mint] = (now, out)
    return out

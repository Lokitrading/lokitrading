"""Memecoin paper bot: what the tested memecoin rule holds now, its tested record, and its record since.

Read-only and paper only. Prices are Binance's public 4-hour (or daily) bars. Nothing here trades:
the rule did not pass its own test (see ``loki_v8/memecoin_record.json``), so it is shown and
tracked on paper, never sent to a broker.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, HTTPException, Response

from loki.api.binance_market_data import MarketDataError, get_binance_spot_history
from loki.api.fusion import _private_response
from loki_v8 import memecoin as mc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/fusion/ai", tags=["Loki Fusion"])

RECORD_PATH = Path(__file__).resolve().parents[2] / "loki_v8" / "memecoin_record.json"
HISTORY_BARS = 1000  # enough for the slowest rule in the grid plus the 30-day volatility window
CACHE_SECONDS = 300.0
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_lock = threading.Lock()


def record() -> Dict[str, Any]:
    try:
        loaded: Dict[str, Any] = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
        return loaded
    except (OSError, ValueError):
        return {}


def _closes(coin: str, bars: str) -> Optional[pd.Series]:
    try:
        candles = get_binance_spot_history(f"{coin}USDT", bars, HISTORY_BARS)
    except MarketDataError as error:
        logger.warning("memecoin bars %s failed: %s", coin, error)
        return None
    index = pd.to_datetime([c["time"] for c in candles], unit="s", utc=True)
    return pd.Series([c["close"] for c in candles], index=index, name=coin)


def plan() -> Dict[str, Any]:
    rec = record()
    chosen = (rec.get("chosen") or {}).get("rule")
    if not chosen:
        raise HTTPException(503, "The memecoin rule has no tested record on this server yet.")
    bars = chosen["bars"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        series = dict(zip(mc.COINS, pool.map(lambda c: _closes(c, bars), mc.COINS)))
    closes = {c: s for c, s in series.items() if s is not None and len(s) > 0}
    if not closes:
        raise HTTPException(503, "Binance's public prices could not be read right now; try again shortly.")
    holdings = mc.live_state(closes, chosen)
    for coin in mc.COINS:
        if coin not in closes:
            holdings.append({"coin": coin, "status": "no_data", "weight": 0.0, "holding": False})
    since = str(rec.get("generated_at") or "")[:10]
    returns = mc.book_returns(closes, chosen)
    after = returns[returns.index > pd.Timestamp(since, tz="UTC")] if since else returns.iloc[0:0]
    forward = mc.stats(after, bars) if len(after) >= 2 else {}
    return {
        "rule": chosen, "label": (rec.get("chosen") or {}).get("label"), "bars": bars,
        "holdings": holdings, "in_market_pct": round(sum(h.get("weight", 0.0) for h in holdings) * 100, 1),
        "forward": {"since": since, "bars": int(len(after)), **forward},
        "record": {k: rec.get(k) for k in ("generated_at", "choose_until", "test_from", "passed", "pass_rule", "chosen_test_rank",
                                           "cost_per_side_pct", "vol_target_pct", "caveats", "first_bar", "per_coin_test")}
        | {"test": (rec.get("chosen") or {}).get("test"), "choose": (rec.get("chosen") or {}).get("choose"), "hold": rec.get("hold"),
           "rules_tested": len(rec.get("all_rules") or []),
           "rules_losing_in_test": sum(1 for r in rec.get("all_rules") or [] if (r.get("test") or {}).get("cagr_pct", 0) < 0),
           "rules": [{"label": r.get("label"), "choose": r.get("choose"), "test": r.get("test")} for r in rec.get("all_rules") or []]},
        "paper_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.get("/memecoin")
def ai_memecoin(response: Response) -> Dict[str, Any]:
    """The memecoin paper bot: current holdings, tested record and record since it was frozen. Paper only."""
    _private_response(response)
    now = time.monotonic()
    with _lock:
        cached = _cache.get("plan")
    if not cached or now - cached[0] > CACHE_SECONDS:
        cached = (now, plan())
        with _lock:
            _cache["plan"] = cached
    return {**cached[1], "served_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

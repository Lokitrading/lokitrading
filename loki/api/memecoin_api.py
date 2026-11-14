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
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, HTTPException, Response

from loki.api.binance_market_data import MarketDataError, _fetch_json, get_binance_spot_history
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
    per_year = mc.BARS_PER_YEAR[bars]
    for h in holdings:
        s = closes.get(h["coin"])
        if s is not None and len(s) > 10:
            h.update(mc.levels(s, chosen))
            tail = s.pct_change().iloc[-(mc.VOL_WINDOW_DAYS * per_year // 365):]
            h["vol_pct_year"] = round(float(tail.std() * (per_year ** 0.5)) * 100, 1)
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
                                           "cost_per_side_pct", "vol_target_pct", "caveats", "first_bar", "per_coin_test",
                                           "walk_forward", "alpaca_spreads_pct")}
        | {"test": (rec.get("chosen") or {}).get("test"), "choose": (rec.get("chosen") or {}).get("choose"), "hold": rec.get("hold"),
           "rules_tested": len(rec.get("all_rules") or []),
           "rules_losing_in_test": sum(1 for r in rec.get("all_rules") or [] if (r.get("test") or {}).get("cagr_pct", 0) < 0),
           "rules": [{"label": r.get("label"), "choose": r.get("choose"), "test": r.get("test")} for r in rec.get("all_rules") or []]},
        "paper_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


SCAN_SECONDS = 15.0


def scan() -> Dict[str, Any]:
    """Live market read for each coin: one 24-hour ticker call for all, order books in parallel."""
    symbols = [f"{c}USDT" for c in mc.COINS]

    def book(symbol: str) -> Optional[Dict[str, Any]]:
        try:
            depth = _fetch_json("/api/v3/depth", {"symbol": symbol, "limit": 100})
            bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
        except (MarketDataError, AttributeError, TypeError, ValueError):
            return None
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2
        bid_usd = sum(p * q for p, q in bids if p >= mid * 0.99)
        ask_usd = sum(p * q for p, q in asks if p <= mid * 1.01)
        return {"spread_bps": round((asks[0][0] - bids[0][0]) / mid * 1e4, 2), "bid_depth_usd": round(bid_usd), "ask_depth_usd": round(ask_usd),
                "imbalance": round((bid_usd - ask_usd) / (bid_usd + ask_usd), 3) if bid_usd + ask_usd > 0 else None}

    with ThreadPoolExecutor(max_workers=7) as pool:  # the ticker and the six order books at once
        ticker_job = pool.submit(_fetch_json, "/api/v3/ticker/24hr", {"symbols": json.dumps(symbols, separators=(",", ":"))})
        books = dict(zip(symbols, pool.map(book, symbols)))
        try:
            tickers = ticker_job.result()
        except MarketDataError as error:
            raise HTTPException(503, "Binance's public prices could not be read right now; try again shortly.") from error
    by_symbol = {t.get("symbol"): t for t in tickers if isinstance(t, dict)} if isinstance(tickers, list) else {}
    try:
        held = {h["coin"]: h for h in _cached_plan()["holdings"]}
    except HTTPException:
        held = {}
    rows: List[Dict[str, Any]] = []
    for coin, symbol in zip(mc.COINS, symbols):
        t, b, h = by_symbol.get(symbol) or {}, books.get(symbol) or {}, held.get(coin) or {}
        try:
            last, change, volume = float(t["lastPrice"]), float(t["priceChangePercent"]), float(t["quoteVolume"])
            trades = int(t["count"])
        except (KeyError, TypeError, ValueError):
            rows.append({"coin": coin, "status": "no_data"})
            continue
        depth = min(b["bid_depth_usd"], b["ask_depth_usd"]) if b else None
        enter, exit_ = h.get("enter_above"), h.get("exit_below")
        rows.append({"coin": coin, "price": last, "change_24h_pct": round(change, 2), "volume_24h_usd": round(volume), "trades_24h": trades,
                     **b, "rule": "hold" if h.get("holding") else "cash", "vol_pct_year": h.get("vol_pct_year"),
                     "to_buy_pct": round((enter / last - 1) * 100, 1) if enter and not h.get("holding") else None,
                     "to_sell_pct": round((exit_ / last - 1) * 100, 1) if exit_ and h.get("holding") else None,
                     "read": mc.read(b.get("spread_bps"), depth, b.get("imbalance"), change)})
    return {"coins": rows, "source": "Binance public market data (24-hour ticker and order book)",
            "note": "A description of the market right now, not a forecast. Trigger distances are from the last completed 4-hour close.",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _cached_plan() -> Dict[str, Any]:
    now = time.monotonic()
    with _lock:
        cached = _cache.get("plan")
    if not cached or now - cached[0] > CACHE_SECONDS:
        cached = (now, plan())
        with _lock:
            _cache["plan"] = cached
    return cached[1]


@router.get("/memecoin/scan")
def ai_memecoin_scan(response: Response) -> Dict[str, Any]:
    """Live read of each memecoin's market: 24-hour figures, spread, order-book depth and the rule's trigger levels."""
    _private_response(response)
    now = time.monotonic()
    with _lock:
        cached = _cache.get("scan")
    if not cached or now - cached[0] > SCAN_SECONDS:
        cached = (now, scan())
        with _lock:
            _cache["scan"] = cached
    return cached[1]


@router.get("/memecoin")
def ai_memecoin(response: Response) -> Dict[str, Any]:
    """The memecoin paper bot: current holdings, tested record and record since it was frozen. Paper only."""
    _private_response(response)
    return {**_cached_plan(), "served_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

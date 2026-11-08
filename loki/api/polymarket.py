"""Polymarket prediction markets: read-only odds, plus "Crowd vs Machine".

Polymarket's public Gamma API is read through this relay (fixed host, short
cache, size-capped, whitelisted fields), so the browser never calls a third
party and the page's CSP stays `connect-src 'self'`.

Nothing here can trade. Real-money Polymarket orders need a crypto wallet's
private key and are geo-restricted in several countries; Loki deliberately
offers only live odds and browser-side paper predictions.

"Crowd vs Machine" turns Polymarket's price ladders ("Bitcoin above ___ on
November 8?", "NVIDIA closes above ___ on November 9?") into the crowd's
implied view of where a market will finish, and puts it next to the trading
AI's own read of the same market.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException, Query, Response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/polymarket", tags=["Prediction markets"])

GAMMA = "https://gamma-api.polymarket.com"
SITE = "https://polymarket.com/event/"
CACHE_SECONDS = 90.0
MAX_BYTES = 8_000_000  # 80 stock events is ~4.7 MB of JSON
CATEGORIES = {
    "finance": "finance", "crypto": "crypto", "economy": "economy", "fed": "fed",
    "stocks": "stocks", "politics": "politics", "trending": None,
}

_cache: Dict[str, Tuple[float, Any]] = {}
_lock = threading.Lock()


class PolymarketError(RuntimeError):
    pass


def _get(path: str, params: Dict[str, Any]) -> Any:
    """One cached, size-capped GET to Polymarket's public Gamma API."""
    key = path + "?" + json.dumps(params, sort_keys=True)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        with requests.get(GAMMA + path, params=params, timeout=(4, 10), allow_redirects=False, stream=True,
                          headers={"Accept": "application/json", "User-Agent": "LokiWorkspace/2.0 (read-only odds)"}) as res:
            if res.status_code != 200:
                raise PolymarketError(f"Polymarket answered HTTP {res.status_code}")
            chunks, size = [], 0
            for chunk in res.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise PolymarketError("Polymarket's answer was larger than expected")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
    except (requests.RequestException, ValueError) as exc:
        raise PolymarketError("Polymarket is unreachable right now") from exc
    with _lock:
        _cache[key] = (now, data)
        if len(_cache) > 200:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                _cache.pop(old, None)
    return data


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _yes_price(market: Dict[str, Any]) -> Optional[float]:
    try:
        prices = json.loads(market.get("outcomePrices") or "[]")
        outcomes = json.loads(market.get("outcomes") or "[]")
    except (TypeError, ValueError):
        return None
    if not prices:
        return None
    index = outcomes.index("Yes") if "Yes" in outcomes else 0
    price = _num(prices[index]) if index < len(prices) else None
    return min(1.0, max(0.0, price)) if price is not None else None


def _clean_market(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if m.get("closed") or not m.get("active", True):
        return None
    yes = _yes_price(m)
    if yes is None:
        return None
    return {
        "id": str(m.get("id") or ""),
        "label": str(m.get("groupItemTitle") or m.get("question") or "")[:90],
        "question": str(m.get("question") or "")[:200],
        "yes": round(yes, 4),
        "change_1d": _num(m.get("oneDayPriceChange")),
        "volume_24h": _num(m.get("volume24hr")),
        "end": m.get("endDate"),
    }


def _clean_event(e: Dict[str, Any], top: int = 6) -> Optional[Dict[str, Any]]:
    if e.get("closed"):
        return None
    markets = [m for m in (_clean_market(m) for m in e.get("markets") or []) if m]
    if not markets:
        return None
    is_ladder = bool(ladder_symbol(str(e.get("title") or "")))
    if not is_ladder:  # outcomes like "No change" / "25 bps hike": most likely first
        markets = sorted(markets, key=lambda m: m["yes"], reverse=True)
    markets = markets[:top]
    slug = re.sub(r"[^a-z0-9-]", "", str(e.get("slug") or ""))[:120]
    return {
        "id": str(e.get("id") or ""),
        "title": str(e.get("title") or "")[:160],
        "url": SITE + slug if slug else None,
        "end": e.get("endDate"),
        "volume_24h": _num(e.get("volume24hr")),
        "volume": _num(e.get("volume")),
        "liquidity": _num(e.get("liquidity")),
        "markets": markets,
        "more": max(0, len(e.get("markets") or []) - len(markets)),
    }


def events(category: str = "finance", limit: int = 24) -> List[Dict[str, Any]]:
    tag = CATEGORIES.get(category)
    params: Dict[str, Any] = {"active": "true", "closed": "false", "limit": min(60, limit * 2),
                              "order": "volume24hr", "ascending": "false"}
    if tag:
        params["tag_slug"] = tag
    rows = _get("/events", params)
    out = [ev for ev in (_clean_event(e) for e in rows if isinstance(e, dict)) if ev]
    return out[:limit]


def search(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    data = _get("/public-search", {"q": query[:80], "limit_per_type": limit, "events_status": "active"})
    rows = data.get("events") if isinstance(data, dict) else None
    return [ev for ev in (_clean_event(e) for e in rows or [] if isinstance(e, dict)) if ev][:limit]


def market(market_id: str) -> Optional[Dict[str, Any]]:
    rows = _get("/markets", {"id": market_id})
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        m = rows[0]
        yes = _yes_price(m)
        return {"id": str(m.get("id")), "question": str(m.get("question") or "")[:200], "yes": yes,
                "closed": bool(m.get("closed")), "end": m.get("endDate")}
    return None


# --- Crowd vs Machine ----------------------------------------------------------
_CRYPTO_NAMES = {"bitcoin": "BTC-USD", "ethereum": "ETH-USD", "solana": "SOL-USD", "xrp": "XRP-USD"}
_LADDER = re.compile(r"\babove", re.I)  # "above___" has no word break before the blank
_TICKER_IN_TITLE = re.compile(r"\(([A-Z]{1,5})\)")


def ladder_symbol(title: str) -> Optional[str]:
    """Which market a price-ladder title is about ('NVIDIA (NVDA) closes above ___' -> NVDA)."""
    if not _LADDER.search(title) or "___" not in title:
        return None
    ticker = _TICKER_IN_TITLE.search(title)
    if ticker:
        return ticker.group(1)
    first = title.split()[0].lower()
    return _CRYPTO_NAMES.get(first)


def _threshold(label: str) -> Optional[float]:
    match = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?", label or "")
    if not match:
        return None
    value = _num(match.group(1).replace(",", ""))
    if value is None:
        return None
    return value * (1000 if match.group(2) else 1)


def crowd_curve(markets: List[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """(price level, P(finish above)) sorted by level and made non-increasing.

    Thin ladders can be slightly inconsistent (a higher level priced above a
    lower one); a running minimum removes that noise without inventing data.
    """
    points = sorted((t, m["yes"]) for m in markets if (t := _threshold(m["label"])) is not None)
    curve, floor = [], 1.0
    for level, p in points:
        floor = min(floor, p)
        curve.append((level, floor))
    return curve


def prob_above(curve: List[Tuple[float, float]], price: float) -> Optional[float]:
    if len(curve) < 2:
        return None
    if price <= curve[0][0]:
        return curve[0][1]
    if price >= curve[-1][0]:
        return curve[-1][1]
    for (x0, p0), (x1, p1) in zip(curve, curve[1:]):
        if x0 <= price <= x1:
            return p0 + (p1 - p0) * (price - x0) / (x1 - x0)
    return None


def crowd_median(curve: List[Tuple[float, float]]) -> Optional[float]:
    """Price level where the crowd's P(finish above) crosses 50%."""
    for (x0, p0), (x1, p1) in zip(curve, curve[1:]):
        if p0 >= 0.5 >= p1 and p0 != p1:
            return x0 + (x1 - x0) * (p0 - 0.5) / (p0 - p1)
    return None


def lean(probability_up: Optional[float]) -> str:
    if probability_up is None:
        return "unknown"
    return "bullish" if probability_up >= 0.55 else "bearish" if probability_up <= 0.45 else "neutral"


def machine_lean(analysis: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    plan = (analysis or {}).get("plan") or {}
    if not plan:
        return "unknown", "no AI read available"
    if plan.get("allowed"):
        return "bullish", "BUY setup — every check passes"
    if plan.get("action") == "SELL" or (plan.get("score") or 0) < 0:
        return "bearish", "Stand aside — momentum is pointing down"
    return "neutral", "Wait — not every check passes yet"


def _price_and_read(symbol: str) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    from fastapi import HTTPException as _HTTPException

    from loki.api import ai_paper

    try:
        analysis = ai_paper._analyze(symbol)
    except _HTTPException:
        return None, None
    except Exception as exc:  # noqa: BLE001 - one market's data blip must not sink the panel
        logger.warning("crowd-vs-machine read failed for %s: %s", symbol, exc.__class__.__name__)
        return None, None
    return _num(analysis.get("price")), analysis


def crowd_vs_machine(max_items: int = 8) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    ladders: Dict[str, List[Dict[str, Any]]] = {}
    for tag in ("crypto", "stocks"):
        rows = _get("/events", {"active": "true", "closed": "false", "limit": 80,
                                "order": "volume24hr", "ascending": "false", "tag_slug": tag})
        for e in rows if isinstance(rows, list) else []:
            title = str(e.get("title") or "")
            symbol = ladder_symbol(title)
            if not symbol or e.get("closed"):
                continue
            try:
                end = datetime.fromisoformat(str(e.get("endDate")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (end - now).total_seconds() < 3600:
                continue  # resolving within the hour: no longer a forecast
            event = _clean_event(e, top=200)
            if not event or len(event["markets"]) < 3:
                continue
            ladders.setdefault(symbol, []).append({**event, "_end": end, "symbol": symbol})
    chosen: Dict[str, Dict[str, Any]] = {}
    for symbol, options in ladders.items():
        # A ladder settling within hours is a coin flip around today's price;
        # prefer the nearest one at least 3 days out, else the nearest at all.
        options.sort(key=lambda ev: ev["_end"])
        later = [ev for ev in options if (ev["_end"] - now).total_seconds() >= 3 * 86400]
        chosen[symbol] = later[0] if later else options[0]
    items: List[Dict[str, Any]] = []
    for symbol, event in sorted(chosen.items(), key=lambda kv: -(kv[1].get("volume_24h") or 0))[:max_items]:
        curve = crowd_curve(event["markets"])
        price, analysis = _price_and_read(symbol)
        if price is None or len(curve) < 3:
            continue
        p_up = prob_above(curve, price)
        median = crowd_median(curve)
        crowd = lean(p_up)
        machine, machine_text = machine_lean(analysis)
        agree = "agree" if crowd == machine and crowd in ("bullish", "bearish") else (
            "disagree" if {crowd, machine} == {"bullish", "bearish"} else "mixed")
        plan = (analysis or {}).get("plan") or {}
        items.append({
            "symbol": symbol, "question": event["title"], "url": event["url"], "resolves": event["end"],
            "price": round(price, 4), "crowd_prob_up": round(p_up, 3) if p_up is not None else None,
            "crowd_median": round(median, 4) if median is not None else None,
            "crowd_median_change_pct": round((median / price - 1) * 100, 2) if median else None,
            "crowd_lean": crowd, "machine_lean": machine, "machine_text": machine_text,
            "machine_score": plan.get("score"), "verdict": agree,
            "horizon_days": round((event["_end"] - now).total_seconds() / 86400, 1),
            "volume_24h": event.get("volume_24h"),
            "curve": [{"level": lvl, "p_above": round(p, 4)} for lvl, p in curve],
        })
    return items


# --- routes ---------------------------------------------------------------------
def _fail(exc: PolymarketError) -> HTTPException:
    return HTTPException(503, f"{exc}. Odds will be back shortly — nothing is guessed in the meantime.")


@router.get("/events")
def api_events(response: Response, category: str = Query(default="finance", pattern="^[a-z]{3,10}$"),
               q: str = Query(default="", max_length=80), limit: int = Query(default=24, ge=1, le=40)) -> Dict[str, Any]:
    if category not in CATEGORIES:
        raise HTTPException(422, "Unknown category.")
    try:
        rows = search(q.strip(), limit) if q.strip() else events(category, limit)
    except PolymarketError as exc:
        raise _fail(exc) from None
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"source": "Polymarket (public data, read-only)", "category": category, "query": q.strip() or None,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "events": rows,
            "note": "Odds are crowd prices, not predictions by Loki. Loki never places Polymarket orders."}


@router.get("/markets")
def api_markets(response: Response, ids: str = Query(min_length=1, max_length=400, pattern=r"^[0-9,]+$")) -> Dict[str, Any]:
    """Current prices for the visitor's paper predictions (comma-separated market ids, max 25)."""
    wanted = [i for i in ids.split(",") if i][:25]
    out = []
    try:
        for market_id in wanted:
            found = market(market_id)
            if found:
                out.append(found)
    except PolymarketError as exc:
        raise _fail(exc) from None
    response.headers["Cache-Control"] = "no-store"
    return {"markets": out}


@router.get("/crowd-vs-machine")
def api_crowd_vs_machine(response: Response) -> Dict[str, Any]:
    try:
        items = crowd_vs_machine()
    except PolymarketError as exc:
        raise _fail(exc) from None
    response.headers["Cache-Control"] = "no-store"
    return {"items": items, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "how_it_works": ("Polymarket traders bet on where each market will finish. Their odds at every price level "
                             "give the crowd's chance of finishing above today's price and its median expected price. "
                             "Loki's trading AI reads the same market from its trend rules. Crowd odds are bets, the AI "
                             "read is a backtested rule — neither is a promise.")}

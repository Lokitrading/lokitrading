"""Polymarket paper bot: model fair value vs the live CLOB, exits first.

What it trades
    Polymarket price ladders ("Bitcoin above ___ on November 11?", "NVIDIA
    closes above ___ on …"). Each rung is a binary market with two separate
    outcome tokens, YES and NO. A view that price finishes ABOVE the strike is
    a BUY of the YES token; a view that it finishes BELOW is a BUY of the NO
    token. Exiting is a SELL of that same token. Nothing is ever shorted.

Fair value
    P(finish above K) = N(d2) with d2 = (ln(S/K) - sigma^2 tau / 2) / (sigma sqrt(tau)):
    live spot S, recent realised volatility sigma, time to resolution tau, zero drift. That is blended half-and-half with the crowd's
    own price (the crowd often knows about earnings, news or quiet weekends the
    model does not). Rungs with a YES spread wider than MAX_SPREAD, long shots
    under 10c and near-certainties over 90c are skipped. A trade needs fair
    value to beat the price actually payable on the order book (asks walked
    level by level for the full size) by at least MIN_EDGE after an estimate
    of Polymarket's taker fee.

Exits are written before entries
    1. lock      — sell when the market is within LOCK_BUFFER of locking
    2. stop      — sell if the token's bid falls STOP_LOSS below entry
    3. target    — sell once most of the remaining upside is in hand
    4. edge gone — sell when the model now values the token below its bid
    Sells walk the bids for the full size, so exit slippage is counted too.

Sizing
    Quarter-Kelly on the edge, capped per position and in total; at most one
    rung per ladder and MAX_PER_SYMBOL per underlying, since rungs of one
    ladder are the same bet.

Deliberately NOT built
    * Real orders. The bot emits orders in Polymarket's CLOB shape (token_id,
      side, price, size, order type) so they can be reviewed, but signing them
      needs a wallet private key, which this server will never hold, and
      Polymarket is geo-restricted in several countries.
    * Latency arbitrage on 5-minute "Up or Down" markets: a serverless website
      cannot win races against co-located bots, so it would only lose fees.
    * Market making: paper fills for resting orders would be invented.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from loki.api import polymarket

router = APIRouter(prefix="/api/polymarket/bot", tags=["Prediction markets"])

CLOB = "https://clob.polymarket.com"
MIN_EDGE = 0.03            # probability points fair value must beat the book by, after spread and fees
KELLY_FRACTION = 0.25
MAX_POSITION_FRACTION = 0.05
MAX_TOTAL_FRACTION = 0.30
MAX_SIGNALS = 8
MAX_PER_SYMBOL = 2
MODEL_WEIGHT = 0.5         # fair value = half model, half crowd: the crowd often knows something
MAX_SPREAD = 0.10          # skip rungs whose YES bid/ask are further apart than this
MIN_ENTRY_PRICE = 0.10     # no long shots (they are systematically overpriced)
MAX_ENTRY_PRICE = 0.90     # no near-certainties (tiny upside against a full stop)
LOCK_BUFFER_SECONDS = 30 * 60
STOP_LOSS = 0.40           # fraction of the entry price
TAKE_PROFIT_SHARE = 0.60   # of the remaining distance to 1.00
MIN_HORIZON_SECONDS = 6 * 3600
MAX_HORIZON_DAYS = 45
BOOK_CACHE_SECONDS = 20.0
MAX_BOOKS_PER_SCAN = 30
_TOKEN = re.compile(r"^\d{10,90}$")

_book_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_vol_cache: Dict[str, Tuple[float, float]] = {}
_lock = threading.Lock()


# --- pure maths (unit-tested) --------------------------------------------------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_prob_above(spot: float, strike: float, sigma: float, tau_years: float) -> Optional[float]:
    """Model chance of finishing above the strike (lognormal, zero drift)."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or tau_years <= 0:
        return None
    s = sigma * math.sqrt(tau_years)
    d2 = (math.log(spot / strike) - 0.5 * s * s) / s
    return min(0.999, max(0.001, norm_cdf(d2)))


def taker_fee_per_share(price: float, base_fee_bps: Optional[float]) -> float:
    """Conservative estimate of Polymarket's taker fee (rate x min(p, 1-p)) per share."""
    if not base_fee_bps:
        return 0.0
    return (float(base_fee_bps) / 10_000.0) * min(price, 1.0 - price)


def walk(levels: List[Tuple[float, float]], shares: float) -> Optional[Tuple[float, float, float]]:
    """Fill `shares` against levels sorted best-first: (avg price, worst price, shares filled)."""
    filled, cost, worst = 0.0, 0.0, None
    for price, size in levels:
        take = min(size, shares - filled)
        if take <= 0:
            break
        filled += take
        cost += take * price
        worst = price
        if filled >= shares - 1e-9:
            break
    if filled <= 0 or worst is None:
        return None
    return cost / filled, worst, filled


def kelly_stake(edge: float, price: float, bankroll: float) -> float:
    """Quarter-Kelly for a binary paying 1: f* = edge / (1 - price)."""
    if edge <= 0 or not 0 < price < 1 or bankroll <= 0:
        return 0.0
    fraction = min(MAX_POSITION_FRACTION, KELLY_FRACTION * edge / (1.0 - price))
    return round(bankroll * fraction, 2)


def exit_decision(position: Dict[str, Any], bid: Optional[float], fair: Optional[float], seconds_to_lock: float) -> Tuple[str, str]:
    """('sell'|'hold', reason) — the exit rules, in priority order."""
    entry = float(position["entry"])
    if seconds_to_lock <= LOCK_BUFFER_SECONDS:
        return "sell", "market locks within 30 minutes — exit before lock"
    if bid is None:
        return "hold", "no bids on the book right now"
    if bid <= entry * (1.0 - STOP_LOSS):
        return "sell", f"stop-loss: bid {bid:.3f} is {STOP_LOSS:.0%} below entry {entry:.3f}"
    if bid >= entry + TAKE_PROFIT_SHARE * (1.0 - entry):
        return "sell", f"take-profit: bid {bid:.3f} captured most of the upside"
    if fair is not None and fair < bid:
        return "sell", f"edge gone: model values the token at {fair:.3f}, below the {bid:.3f} bid"
    return "hold", "edge intact"


# --- live data ------------------------------------------------------------------
def book(token_id: str) -> Dict[str, Any]:
    """Live CLOB book for one outcome token, best levels first."""
    if not _TOKEN.match(token_id):
        raise polymarket.PolymarketError("invalid token id")
    now = time.time()
    with _lock:
        hit = _book_cache.get(token_id)
        if hit and now - hit[0] < BOOK_CACHE_SECONDS:
            return hit[1]
    try:
        res = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=(3, 6), allow_redirects=False)
        raw = res.json() if res.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        raise polymarket.PolymarketError("Polymarket's order book is unreachable") from exc
    if not isinstance(raw, dict):
        raise polymarket.PolymarketError("Polymarket's order book answered unexpectedly")

    def levels(rows: Any, reverse: bool) -> List[Tuple[float, float]]:
        out = []
        for row in rows or []:
            try:
                price, size = float(row["price"]), float(row["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < price < 1 and size > 0:
                out.append((price, size))
        return sorted(out, key=lambda pair: pair[0], reverse=reverse)

    parsed = {"asks": levels(raw.get("asks"), False), "bids": levels(raw.get("bids"), True),
              "tick": float(raw.get("tick_size") or 0.001), "min_size": float(raw.get("min_order_size") or 5)}
    with _lock:
        _book_cache[token_id] = (now, parsed)
        if len(_book_cache) > 400:
            for old in sorted(_book_cache, key=lambda k: _book_cache[k][0])[:100]:
                _book_cache.pop(old, None)
    return parsed


def realized_vol(symbol: str) -> Optional[float]:
    """Annualised volatility from ~30 recent daily closes (cached for an hour)."""
    now = time.time()
    with _lock:
        hit = _vol_cache.get(symbol)
        if hit and now - hit[0] < 3600:
            return hit[1]
    from loki.api import ai_paper
    from loki_v8.data import is_crypto

    pipeline, _cfg, _ = ai_paper._get_pipeline(symbol)
    frame = pipeline.provider.fetch_history(symbol, "3mo", "1d")
    if frame is None or len(frame) < 25:
        return None
    closes = frame["close"].astype(float).tail(31).tolist()
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sigma = math.sqrt(var) * math.sqrt(365 if is_crypto(symbol) else 252)
    with _lock:
        _vol_cache[symbol] = (now, sigma)
    return sigma


def _spot(symbol: str) -> Optional[float]:
    from loki.api.market_feed import quote_or_failure

    row = quote_or_failure(symbol, None, 30.0)
    return float(row["price"]) if row.get("ok") and row.get("price") else None


def _tau_years(seconds: float) -> float:
    """Calendar time in years. Stock vol is annualised over 252 trading days, and
    ~252/365 of calendar days trade, so calendar years are the right clock for both."""
    return seconds / (365.0 * 86400.0)


def _ladders() -> List[Dict[str, Any]]:
    """Raw open ladder events with their rungs' token ids, resolving 6 h – 45 days out."""
    now = datetime.now(timezone.utc)
    out = []
    for tag in ("crypto", "stocks"):
        rows = polymarket._get("/events", {"active": "true", "closed": "false", "limit": 80,
                                           "order": "volume24hr", "ascending": "false", "tag_slug": tag})
        for e in rows if isinstance(rows, list) else []:
            symbol = polymarket.ladder_symbol(str(e.get("title") or ""))
            if not symbol or e.get("closed"):
                continue
            try:
                end = datetime.fromisoformat(str(e.get("endDate")).replace("Z", "+00:00"))
            except ValueError:
                continue
            seconds = (end - now).total_seconds()
            if seconds < MIN_HORIZON_SECONDS or seconds > MAX_HORIZON_DAYS * 86400:
                continue
            rungs = []
            for m in e.get("markets") or []:
                if m.get("closed") or not m.get("acceptingOrders", True):
                    continue
                strike = polymarket._threshold(str(m.get("groupItemTitle") or ""))
                try:
                    tokens = json.loads(m.get("clobTokenIds") or "[]")
                    outcomes = json.loads(m.get("outcomes") or "[]")
                except (TypeError, ValueError):
                    continue
                if strike is None or len(tokens) != 2 or outcomes[:2] != ["Yes", "No"]:
                    continue
                bid, ask = polymarket._num(m.get("bestBid")), polymarket._num(m.get("bestAsk"))
                if bid is None or ask is None or not 0 < bid < ask < 1 or ask - bid > MAX_SPREAD:
                    continue  # no two-sided market worth crossing
                rungs.append({"market_id": str(m.get("id")), "strike": strike, "yes_token": str(tokens[0]),
                              "no_token": str(tokens[1]), "yes_bid": bid, "yes_ask": ask,
                              "fee_bps": polymarket._num(m.get("takerBaseFee")) if m.get("feesEnabled") else 0.0})
            if rungs:
                cleaned = polymarket._clean_event(e, top=1)
                out.append({"symbol": symbol, "title": e.get("title"), "end": end, "seconds": seconds,
                            "url": cleaned["url"] if cleaned else None, "rungs": rungs})
    return out


def _market_inputs(symbols: List[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """(spot, realised vol) per symbol, fetched in parallel; a symbol that fails is skipped."""
    def one(symbol: str) -> Tuple[Optional[float], Optional[float]]:
        try:
            return _spot(symbol), realized_vol(symbol)
        except Exception:  # noqa: BLE001 - one bad feed must not stop the scan
            return None, None

    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        return dict(zip(symbols, pool.map(one, symbols)))


def _books(token_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    def one(token_id: str) -> Optional[Dict[str, Any]]:
        try:
            return book(token_id)
        except polymarket.PolymarketError:
            return None

    if not token_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(10, len(token_ids))) as pool:
        return dict(zip(token_ids, pool.map(one, token_ids)))


def model_prob_above(spot: float, strike: float, sigma: float, seconds: float) -> Optional[float]:
    """The model's own view, before blending with the crowd."""
    return fair_prob_above(spot, strike, sigma, _tau_years(seconds))


def scan(bankroll: float, exposure: float = 0.0) -> Dict[str, Any]:
    """Entry candidates: model fair value vs the price actually payable on the book."""
    ladders = _ladders()
    room = max(0.0, MAX_TOTAL_FRACTION * bankroll - exposure)
    inputs = _market_inputs(sorted({ladder["symbol"] for ladder in ladders}))

    # Pass 1: rungs where the model and the quoted mid already disagree enough to matter.
    wanted: List[Dict[str, Any]] = []
    for ladder in ladders:
        spot, sigma = inputs.get(ladder["symbol"], (None, None))
        if not spot or not sigma:
            continue
        for rung in ladder["rungs"]:
            raw = model_prob_above(spot, rung["strike"], sigma, ladder["seconds"])
            if raw is None:
                continue
            market = (rung["yes_bid"] + rung["yes_ask"]) / 2.0
            fair = MODEL_WEIGHT * raw + (1.0 - MODEL_WEIGHT) * market
            # YES is bought at the YES ask; NO is bought at ~1 - YES bid (the books mirror each other).
            yes_edge, no_edge = fair - rung["yes_ask"], (1.0 - fair) - (1.0 - rung["yes_bid"])
            side_yes = yes_edge >= no_edge
            if max(yes_edge, no_edge) < MIN_EDGE:
                continue
            wanted.append({"ladder": ladder, "rung": rung, "spot": spot, "sigma": sigma, "side_yes": side_yes,
                           "token": rung["yes_token"] if side_yes else rung["no_token"],
                           "model": fair if side_yes else 1.0 - fair, "quoted_edge": max(yes_edge, no_edge),
                           "market": market if side_yes else 1.0 - market})
    wanted.sort(key=lambda w: w["quoted_edge"], reverse=True)
    wanted = wanted[:MAX_BOOKS_PER_SCAN]
    books = _books([w["token"] for w in wanted])

    # Pass 2: price each against the real book, for the full size, after fees.
    candidates: List[Dict[str, Any]] = []
    for w in wanted:
        b = books.get(w["token"])
        if not b or not b["asks"]:
            continue
        ladder, rung, model = w["ladder"], w["rung"], w["model"]
        # Only levels that still clear the edge bar are taken: the limit price is fair value minus MIN_EDGE.
        limit = min(MAX_ENTRY_PRICE, model - MIN_EDGE)
        levels = [(p, q) for p, q in b["asks"] if p + taker_fee_per_share(p, rung["fee_bps"]) <= limit]
        if not levels or levels[0][0] < MIN_ENTRY_PRICE:
            continue
        stake = kelly_stake(model - levels[0][0], levels[0][0], bankroll)
        shares = min(stake / levels[0][0], sum(q for _, q in levels))
        if stake <= 0 or shares < b["min_size"]:
            continue  # not enough size at a price worth paying
        fill = walk(levels, shares)
        if not fill:
            continue
        avg, worst, filled = fill
        fee = taker_fee_per_share(avg, rung["fee_bps"])
        edge = model - avg - fee
        side_yes = w["side_yes"]
        candidates.append({
            "symbol": ladder["symbol"], "question": ladder["title"], "url": ladder["url"], "strike": rung["strike"],
            "resolves": ladder["end"].isoformat().replace("+00:00", "Z"), "market_id": rung["market_id"],
            "token": "YES" if side_yes else "NO", "token_id": w["token"], "thesis": (
                f"finishes above {rung['strike']:,.4g}" if side_yes else f"finishes below {rung['strike']:,.4g}"),
            "spot": round(w["spot"], 4), "sigma": round(w["sigma"], 4), "model_prob": round(model, 4),
            "market_prob": round(w["market"], 4), "avg_fill": round(avg, 4),
            "fee_per_share": round(fee, 4), "edge": round(edge, 4), "shares": round(filled, 2),
            "cost": round(avg * filled + fee * filled, 2),
            "order": {"token_id": w["token"], "side": "BUY", "price": round(worst, 3), "size": round(filled, 2),
                      "order_type": "FOK"},
        })
    return {"signals": choose(candidates, room), "ladders_scanned": len(ladders), "books_checked": len(books),
            "candidates": len(candidates), "room": round(room, 2), "rules": rules()}


def choose(candidates: List[Dict[str, Any]], room: float) -> List[Dict[str, Any]]:
    """Best edges first, within the cash room, at most one rung per ladder and two per symbol
    (rungs of one ladder are the same bet, so stacking them is concentration, not diversification)."""
    chosen: List[Dict[str, Any]] = []
    spent = 0.0
    ladders_used, per_symbol = set(), {}  # type: ignore[var-annotated]
    for c in sorted(candidates, key=lambda c: c["edge"], reverse=True):
        key = (c["symbol"], c["resolves"])
        if (len(chosen) >= MAX_SIGNALS or spent + c["cost"] > room or key in ladders_used
                or per_symbol.get(c["symbol"], 0) >= MAX_PER_SYMBOL):
            continue
        chosen.append(c)
        spent += c["cost"]
        ladders_used.add(key)
        per_symbol[c["symbol"]] = per_symbol.get(c["symbol"], 0) + 1
    return chosen


def review(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Exit decisions for open paper positions, walking the bids for the real exit price."""
    now = datetime.now(timezone.utc)
    inputs = _market_inputs(sorted({str(p["symbol"]) for p in positions}))

    def one(p: Dict[str, Any]) -> Dict[str, Any]:
        try:
            end = datetime.fromisoformat(str(p["resolves"]).replace("Z", "+00:00"))
        except ValueError:
            end = now
        seconds = (end - now).total_seconds()
        bid_avg, mid = None, None
        try:
            b = book(p["token_id"])
            fill = walk(b["bids"], float(p["shares"]))
            bid_avg = fill[0] if fill and fill[2] >= float(p["shares"]) * 0.999 else None
            if b["bids"] and b["asks"]:
                mid = (b["bids"][0][0] + b["asks"][0][0]) / 2.0
        except polymarket.PolymarketError:
            pass
        fair = None
        spot, sigma = inputs.get(p["symbol"], (None, None))
        if spot and sigma and seconds > 0:
            above = model_prob_above(spot, float(p["strike"]), sigma, seconds)
            if above is not None:
                own = above if p["token"] == "YES" else 1.0 - above
                fair = own if mid is None else MODEL_WEIGHT * own + (1.0 - MODEL_WEIGHT) * mid
        action, reason = exit_decision(p, bid_avg, fair, seconds)
        out = {"id": p["id"], "action": action, "reason": reason, "bid": round(bid_avg, 4) if bid_avg else None,
               "model_prob": round(fair, 4) if fair is not None else None}
        if action == "sell" and bid_avg:
            out["order"] = {"token_id": p["token_id"], "side": "SELL", "price": round(bid_avg, 3),
                            "size": float(p["shares"]), "order_type": "FOK"}
        return out

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(positions)))) as pool:
        return list(pool.map(one, positions))


def rules() -> Dict[str, Any]:
    return {"min_edge": MIN_EDGE, "kelly_fraction": KELLY_FRACTION, "model_weight": MODEL_WEIGHT,
            "entry_price_range": [MIN_ENTRY_PRICE, MAX_ENTRY_PRICE],
            "max_per_symbol": MAX_PER_SYMBOL, "max_position_fraction": MAX_POSITION_FRACTION,
            "max_total_fraction": MAX_TOTAL_FRACTION, "lock_buffer_minutes": LOCK_BUFFER_SECONDS // 60,
            "stop_loss": STOP_LOSS, "take_profit_share": TAKE_PROFIT_SHARE}


# --- routes ---------------------------------------------------------------------
class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    token_id: str = Field(pattern=r"^\d{10,90}$")
    token: str = Field(pattern=r"^(YES|NO)$")
    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z0-9.^=\-]+$")
    strike: float = Field(gt=0, lt=1e9)
    entry: float = Field(gt=0, lt=1)
    shares: float = Field(gt=0, lt=1e7)
    resolves: str = Field(min_length=10, max_length=40)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positions: List[Position] = Field(max_length=20)


@router.get("/signals")
def api_signals(response: Response, bankroll: float = Query(default=1000.0, gt=0, le=1e7),
                exposure: float = Query(default=0.0, ge=0, le=1e7)) -> Dict[str, Any]:
    try:
        data = scan(bankroll, exposure)
    except polymarket.PolymarketError as exc:
        raise HTTPException(503, f"{exc}. The bot pauses rather than guess.") from None
    response.headers["Cache-Control"] = "no-store"
    data["paper"] = True
    data["note"] = ("Paper only. Orders are shown in Polymarket's CLOB format for review; this server never holds a "
                    "wallet key and never signs or sends them.")
    return data


@router.post("/review")
def api_review(body: ReviewBody, response: Response) -> Dict[str, Any]:
    try:
        decisions = review([p.model_dump() for p in body.positions])
    except polymarket.PolymarketError as exc:
        raise HTTPException(503, f"{exc}. Positions are held until the book answers.") from None
    response.headers["Cache-Control"] = "no-store"
    return {"paper": True, "decisions": decisions, "rules": rules()}

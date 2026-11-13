"""The Day Trader on the visitor's own Alpaca PAPER account.

Each call is one check: read the Day Trader's plan (the same tested rules as the
AI trading page), read the paper account, and send the orders that make the
account hold what the rules hold. The browser calls it every 30 seconds while
the visitor's tab is open, and again just after each half hour, when the rules
decide. Nothing is stored here: the keys arrive with each call and go only to
Alpaca's paper endpoint.

Guards, all checked on every call:

* Paper only. The host is pinned to ``paper-api.alpaca.markets``.
* Only the Day Trader's own markets are touched. At start, the account must not
  already hold any of them, so an existing position is never closed by mistake.
* At least US$2,000 of equity, the margin-account minimum shorts need. (FINRA's
  US$25,000 pattern-day-trader minimum ended on 4 June 2026; brokers phase in the new
  intraday-margin checks until October 2027, so a broker refusal for buying power or
  margin is reported and retried on the next check, never treated as bad keys.)
* New positions never use more than 95% of the buying power Alpaca reports.
* Everything is closed from 10 minutes before Alpaca's own closing time (which also
  covers early-close days), so an API hiccup at the bell cannot leave a position overnight.
* A daily loss stop: if the account is down 2% or more since yesterday's close,
  every Day Trader position is closed and no new one is opened that day. In the
  10-year test the book fell that far on one day out of about 2,700.
* Whole shares, at most US$50,000 and 5,000 shares per market. A short is opened
  only in a stock Alpaca lists as easy to borrow.
* A position is only opened or closed when the rules change direction. It is not
  resized during the day, so one check never fights another.
* A new position opens only within 5 minutes of the rules' signal (the delay the
  test measured); a missed signal waits for the next one. Closing never expires.
* No new position while the quoted spread is wider than 0.10% of the price.
* Every Alpaca answer is checked against a schema before it is used; a malformed
  answer trades nothing. Sizes are computed in exact decimals.
* Every order carries a deterministic client order id, so a retried request can
  never place the same order twice, and a receipt: a SHA-256 hash of the market
  snapshot (with the quote feed it came from), the risk policy, the drift state and
  the checks it passed. A blocked order gets a receipt too, saying which check stopped
  it. The page adds each receipt to the device's signed, hash-chained trade log.
* A drift alarm (loki_v8/drift.py) compares the account's own recent round trips and
  fills with the tested record and shrinks new positions to x0.5 or x0.1 when they drift.
* Orders are whole shares, market, day only: no fractional shares, brackets, or
  limit or stop prices, so tick-size and fractional-order rejections cannot occur.
* Every check is a fresh reconciliation: positions and open orders are read from Alpaca
  before anything is decided, so a restarted server or a closed tab never orphans one.

Results are not guaranteed. The rules made money on the years they never saw,
but they lost money on most of the markets tested, and late fills cost a lot.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from loki.api.broker_alpaca import (
    _DATA_BASE,
    AlpacaPaperKeys,
    _exchange,
    _number_or_none,
    _order_view,
    _paper_exchange,
    _require_gate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/broker/alpaca", tags=["Broker relay"])

# --- The risk policy (every limit in one place; its hash goes into each receipt) ---------------
POLICY: Dict[str, Any] = {
    "version": 2,
    "venue": "alpaca-paper",
    "min_equity_usd": 2_000,  # margin-account minimum (shorts need margin)
    "max_buying_power_used": "0.95",  # new positions never use more of Alpaca's reported buying power
    "flatten_minutes_before_close": 10,  # close everything this long before Alpaca's closing time
    "quote_max_age_seconds": 120,  # an older quote is not clean
    "drift": {"min_trades": 20, "window": 30, "amber": "z<-1.5 or payoff<60% or slippage>2x cost: size x0.5",
              "red": "z<-2.5 or payoff<35%: size x0.1"},
    "daily_loss_stop_pct": 2.0,  # close everything and stop for the day at this loss
    "max_per_market_usd": 50_000,  # cap on one market's position
    "max_shares": 5_000,
    "size_buffer": "0.95",  # leave room for the price moving between the check and the fill
    "signal_ttl_seconds": 300,  # a new position opens only this soon after the rules' signal
    "max_spread_pct": 0.10,  # no new position while the quoted spread is wider than this
    "shorts": "easy-to-borrow only",
    "order_type": "market, day, regular hours",
}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
MIN_EQUITY = float(POLICY["min_equity_usd"])
DAILY_LOSS_STOP_PCT = float(POLICY["daily_loss_stop_pct"])
MAX_PER_MARKET = Decimal(POLICY["max_per_market_usd"])
MAX_SHARES = int(POLICY["max_shares"])
SIZE_BUFFER = Decimal(POLICY["size_buffer"])
SIGNAL_TTL_SECONDS = int(POLICY["signal_ttl_seconds"])
BUYING_POWER_USE = Decimal(POLICY["max_buying_power_used"])
FLATTEN_MINUTES = int(POLICY["flatten_minutes_before_close"])
QUOTE_MAX_AGE = int(POLICY["quote_max_age_seconds"])
DRIFT_LOOKBACK_DAYS = 45
MAX_SPREAD_PCT = float(POLICY["max_spread_pct"])
FLAT_WAIT_SECONDS = (1.0, 1.5, 2.0)  # after closing, how long to wait for flat before reversing


# --- What Alpaca must send before anything is traded --------------------------------------------
def _finite(value: Any) -> float:
    number = _number_or_none(value)
    if number is None:
        raise ValueError("not a finite number")
    return number


class AlpacaAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")
    equity: float = Field(ge=0)
    last_equity: float = Field(ge=0)
    buying_power: float = Field(ge=0)
    trading_blocked: bool = False
    account_blocked: bool = False

    @field_validator("equity", "last_equity", "buying_power", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    is_open: bool
    timestamp: Optional[str] = None
    next_open: Optional[str] = None
    next_close: Optional[str] = None

    def minutes_to_close(self) -> Optional[float]:
        try:
            return (datetime.fromisoformat(str(self.next_close)) - datetime.fromisoformat(str(self.timestamp))).total_seconds() / 60
        except (TypeError, ValueError):
            return None


class AlpacaPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str = Field(min_length=1, max_length=12)
    qty: float
    side: Literal["long", "short"]

    @field_validator("qty", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ap: float = Field(gt=0)  # ask
    bp: float = Field(gt=0)  # bid
    t: Optional[str] = None  # quote time

    @field_validator("ap", "bp", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaFill(BaseModel):
    """One fill from the account's activity feed (for the drift alarm)."""

    model_config = ConfigDict(extra="ignore")
    symbol: str
    side: str
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    transaction_time: str

    @field_validator("qty", "price", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


def _valid(model: Any, payload: Any, what: str) -> Any:
    """An Alpaca answer, checked against its schema; a malformed one stops the check before any trade."""
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        logger.warning(json.dumps({"event": "alpaca_malformed", "what": what, "errors": error.error_count()}))
        raise HTTPException(502, f"Alpaca sent a malformed {what}; nothing was traded on this check.") from error


class AlpacaDayTradeCheck(AlpacaPaperKeys):
    """One check of the Day Trader on this paper account."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal["day trade paper money"]
    start: bool = False  # the first check: refuse if the account already holds the Day Trader's markets
    stop: bool = False  # close every Day Trader position now and trade no more


def signed_shares(position: Optional[Dict[str, Any]]) -> int:
    """Shares held: positive long, negative short, 0 for none."""
    if not position:
        return 0
    try:
        qty = abs(float(position.get("qty") or 0))
    except (TypeError, ValueError):
        return 0
    qty = int(round(qty))
    return -qty if str(position.get("side") or "").lower() == "short" else qty


def signal_age(market: Dict[str, Any], now: str) -> Optional[float]:
    """Seconds since the rules decided the market's current stance (None when unknown)."""
    try:
        return (datetime.fromisoformat(str(now)) - datetime.fromisoformat(str(market.get("signal_at")))).total_seconds()
    except (TypeError, ValueError):
        return None


def client_order_id(symbol: str, signal_at: Any, shares: int, ref_price: float = 0.0) -> str:
    """The same signal always gives the same id, so Alpaca refuses a second copy of an order.

    The id also carries the price the rules decided on (the decision bar's close, in cents),
    so the drift alarm can later measure each fill's slippage from Alpaca's own records.
    """
    stamp = "".join(ch for ch in str(signal_at or "unknown") if ch.isalnum())[:16]
    cents = int(round(ref_price * 100)) if ref_price and ref_price > 0 else 0
    return f"loki-dt-{symbol}-{stamp}-{'buy' if shares > 0 else 'sell'}-r{cents}"


def slippage_from_orders(orders: List[Dict[str, Any]]) -> List[float]:
    """Basis points each Day Trader entry filled against itself, from its id's decision price."""
    out = []
    for order in orders:
        cid = str(order.get("client_order_id") or "")
        if not cid.startswith("loki-dt-") or "-r" not in cid:
            continue
        try:
            ref = int(cid.rsplit("-r", 1)[1]) / 100
            fill = float(order.get("filled_avg_price") or 0)
        except ValueError:
            continue
        if ref <= 0 or fill <= 0:
            continue
        sign = 1 if "-buy-" in cid else -1
        out.append(round(sign * (fill - ref) / ref * 1e4, 2))
    return out


def receipt(trace_id: str, symbol: str, action: str, shares: int, snapshot: Dict[str, Any], checks: List[str]) -> Dict[str, Any]:
    """What was known and checked when an order was sent, sealed with a SHA-256 hash."""
    body = {"trace_id": trace_id, "symbol": symbol, "action": action, "shares": shares, "policy": POLICY_HASH,
            "snapshot": snapshot, "checks_passed": checks, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {**body, "hash": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def wanted_side(market: Dict[str, Any]) -> int:
    """What the rules want this market to hold now: 1 long, -1 short, 0 flat."""
    if market.get("status") != "open" or market.get("waiting") or market.get("skipped"):
        return 0
    pending = market.get("pending")
    side = pending if pending is not None else market.get("position")
    try:
        side = int(side or 0)
    except (TypeError, ValueError):
        return 0
    return max(-1, min(1, side))


def target_shares(side: int, equity: float, weight: float, price: float) -> int:
    """Whole shares for one market's equal share of the account, within the caps."""
    try:
        equity_d, weight_d, price_d = Decimal(str(equity)), Decimal(str(weight)), Decimal(str(price))
    except InvalidOperation:
        return 0
    if side == 0 or not price_d.is_finite() or not equity_d.is_finite() or price_d <= 0 or equity_d <= 0 or weight_d <= 0:
        return 0
    share = (equity_d * weight_d).quantize(Decimal("0.01"))  # to the cent, so 1/6 of $120,000 is $20,000.00
    budget = min(share, MAX_PER_MARKET) * SIZE_BUFFER
    return side * min(int((budget / price_d).to_integral_value(rounding=ROUND_FLOOR)), MAX_SHARES)


def plan_orders(markets: List[Dict[str, Any]], held: Dict[str, int], equity: float, weight: float,
                busy: set, shortable: Dict[str, bool], stop: bool, now: str = "") -> List[Dict[str, Any]]:
    """For each market, what to do now. Pure: no network, so it can be tested on its own."""
    rows = []
    for market in markets:
        symbol = str(market["symbol"])
        price = _number_or_none(market.get("now_price")) or _number_or_none(market.get("last")) or 0.0
        side = 0 if stop else wanted_side(market)
        note = ""
        if side < 0 and not shortable.get(symbol, False):
            side, note = 0, "the rules are short, but Alpaca does not list this stock as easy to borrow, so it stays flat"
        target = target_shares(side, equity, weight, price)
        if side and target == 0:
            note = "one share costs more than this market's share of the account"
        holding = held.get(symbol, 0)
        if symbol in busy:
            action, note = "wait", "an order for this market is still being filled"
        elif holding == 0 and target == 0:
            action = "none"
        elif holding == 0:
            action = "open"
        elif target == 0 or (holding > 0) != (target > 0):
            action = "close_then_open" if target else "close"
        else:
            action = "hold"  # same direction: positions are not resized during the day
        age = signal_age(market, now) if now else None
        if action in ("open", "close_then_open") and age is not None and age > SIGNAL_TTL_SECONDS:
            action = "close" if action == "close_then_open" else "none"
            note = f"missed this signal ({int(age // 60)} min old); it waits for the rules' next decision"
        rows.append({"symbol": symbol, "rules": side, "price": round(price, 4), "held": holding, "target": target,
                     "action": action, "note": note, "signal_at": market.get("signal_at"),
                     "ref_price": _number_or_none(market.get("last")) or price,
                     "cost_bps": _number_or_none(market.get("cost_bps")) or 2.0})
    return rows


def _held(keys: AlpacaPaperKeys, symbols: List[str]) -> Dict[str, int]:
    raw = _paper_exchange("GET", "/v2/positions", keys)
    if not isinstance(raw, list):
        raise HTTPException(502, "Alpaca's positions answer was not the expected list.")
    positions = [_valid(AlpacaPosition, p, "position") for p in raw]
    return {p.symbol: signed_shares(p.model_dump()) for p in positions if p.symbol in symbols}


def _spread(keys: AlpacaPaperKeys, symbol: str, now: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The latest clean quote: bid, ask, spread (% of mid), the feed it came from and its age.

    The consolidated SIP feed is tried first (it needs a paid Alpaca data plan); otherwise the
    free IEX feed, which carries a small share of volume and can show a wider spread than the
    whole market. The feed is recorded in every receipt so missed entries can be traced to it.
    """
    for feed in ("sip", "iex"):
        try:
            raw = _exchange("GET", f"/v2/stocks/{symbol}/quotes/latest?feed={feed}", keys, None, _DATA_BASE)
            quote = AlpacaQuote.model_validate((raw or {}).get("quote") if isinstance(raw, dict) else None)
        except (HTTPException, ValidationError):
            continue
        if quote.ap < quote.bp:
            continue
        age = None
        try:
            age = (datetime.fromisoformat(str(now)) - datetime.fromisoformat(str(quote.t).replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            pass
        if age is not None and age > QUOTE_MAX_AGE:
            continue
        mid = (quote.ap + quote.bp) / 2
        return {"bid": quote.bp, "ask": quote.ap, "spread_pct": round((quote.ap - quote.bp) / mid * 100, 4),
                "feed": feed.upper(), "quote_age_s": None if age is None else round(age, 1)}
    return None


def _drift(keys: AlpacaPaperKeys, symbols: List[str], markets: List[Dict[str, Any]],
           baselines: Dict[str, Dict[str, Any]], today: str) -> Dict[str, Any]:
    """The drift alarm from the account's own fills and orders over the last weeks."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from loki_v8 import drift

    after = (datetime.fromisoformat(today) - timedelta(days=DRIFT_LOOKBACK_DAYS)).date().isoformat()
    try:
        raw_fills = _paper_exchange("GET", f"/v2/account/activities/FILL?after={after}&direction=asc&page_size=100", keys)
        raw_orders = _paper_exchange("GET", f"/v2/orders?status=closed&after={after}T00:00:00Z&limit=500&direction=asc", keys)
    except HTTPException as error:
        if error.status_code == 401:
            raise
        return {"tier": "green", "multiplier": 1.0, "trades": 0, "reasons": ["drift data unavailable this check; size stays x1.0"]}
    ny = ZoneInfo("America/New_York")
    fills = []
    for item in raw_fills if isinstance(raw_fills, list) else []:
        try:
            fill = AlpacaFill.model_validate(item)
        except ValidationError:
            continue  # one malformed fill is left out, never guessed
        day = datetime.fromisoformat(fill.transaction_time.replace("Z", "+00:00")).astimezone(ny).date().isoformat()
        fills.append({"symbol": fill.symbol, "side": fill.side, "qty": fill.qty, "price": fill.price, "day": day})
    trips = [t for t in drift.round_trips(fills, symbols) if t["day"] < today]  # finished days only
    slippage = slippage_from_orders([o for o in raw_orders if isinstance(o, dict)] if isinstance(raw_orders, list) else [])
    costs = [_number_or_none(m.get("cost_bps")) or 2.0 for m in markets]
    return drift.assess(trips, baselines, slippage, sum(costs) / len(costs) if costs else 2.0)


def _order(keys: AlpacaPaperKeys, symbol: str, shares: int, order_id: str) -> Dict[str, Any]:
    body = {"symbol": symbol, "qty": str(abs(shares)), "side": "buy" if shares > 0 else "sell",
            "type": "market", "time_in_force": "day", "extended_hours": False, "client_order_id": order_id}
    try:
        payload = _paper_exchange("POST", "/v2/orders", keys, body)
    except HTTPException as error:
        if error.status_code == 422 and "client_order_id" in str(error.detail):
            return {"client_order_id": order_id, "status": "already sent"}  # a retry: Alpaca has this order
        raise
    return {**(_order_view(payload) if isinstance(payload, dict) else {}), "client_order_id": order_id}


def _check(body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    from loki.api import ai_paper

    trace_id = uuid.uuid4().hex[:16]
    keys = AlpacaPaperKeys(key_id=body.key_id, secret_key=body.secret_key)
    account = _valid(AlpacaAccount, _paper_exchange("GET", "/v2/account", keys), "account")
    if account.trading_blocked or account.account_blocked:
        raise HTTPException(422, "Alpaca has blocked trading on this paper account.")
    equity = account.equity
    last_equity = account.last_equity or equity
    day_change_pct = (equity / last_equity - 1.0) * 100 if last_equity > 0 else 0.0
    if not body.stop and equity < MIN_EQUITY:
        raise HTTPException(422, f"Day trading needs at least US${MIN_EQUITY:,.0f} in the account (the margin-account minimum "
                                 f"that short sales need). This paper account has US${equity:,.2f}. "
                                 "You can reset a paper account to a larger balance in the Alpaca dashboard.")
    clock = _valid(AlpacaClock, _paper_exchange("GET", "/v2/clock", keys), "clock")
    market_open = clock.is_open
    to_close = clock.minutes_to_close() if market_open else None
    closing_time = to_close is not None and to_close <= FLATTEN_MINUTES

    plan = ai_paper.ai_daytrade(Response(), mode="stocks")
    markets = list(plan.get("markets") or [])
    symbols = [str(m["symbol"]) for m in markets]
    held = _held(keys, symbols)
    if body.start and any(held.values()):
        listed = ", ".join(f"{s} ({q:+d} shares)" for s, q in held.items() if q)
        raise HTTPException(409, f"This paper account already holds {listed}. The Day Trader trades those markets itself and "
                                 "would close them. Close them first, or use a separate paper account (Alpaca lets you "
                                 "open several).")
    stopped_for_loss = day_change_pct <= -DAILY_LOSS_STOP_PCT
    stop = body.stop or stopped_for_loss or closing_time
    record = ai_paper._daytrade_record()
    today = str(plan.get("served_at") or datetime.now(timezone.utc).isoformat())[:10]
    drift_state = _drift(keys, symbols, markets, record.get("market_baselines") or {}, today)
    multiplier = Decimal(str(drift_state["multiplier"]))

    raw_orders = _paper_exchange("GET", "/v2/orders?status=open&limit=100", keys)
    busy = {str(o.get("symbol")) for o in raw_orders if isinstance(o, dict)} if isinstance(raw_orders, list) else set()
    shortable: Dict[str, bool] = {}
    for market in markets:
        if wanted_side(market) < 0 and not stop:
            asset = _paper_exchange("GET", f"/v2/assets/{market['symbol']}", keys)
            shortable[str(market["symbol"])] = bool(isinstance(asset, dict) and asset.get("shortable") and asset.get("easy_to_borrow"))
    rows = plan_orders(markets, held, equity, float(plan.get("weight_per_market") or 0), busy, shortable, stop,
                       now=str(plan.get("served_at") or ""))
    for row in rows:  # the drift alarm only ever shrinks new positions
        if row["target"] and multiplier < 1:
            shrunk = int((Decimal(abs(row["target"])) * multiplier).to_integral_value(rounding=ROUND_FLOOR))
            row["target"] = shrunk if row["target"] > 0 else -shrunk
            if row["target"] == 0 and row["action"] in ("open", "close_then_open"):
                row["action"] = "close" if row["action"] == "close_then_open" else "none"
    if closing_time:
        for row in rows:
            if row["held"]:
                row["note"] = f"closing: {FLATTEN_MINUTES} minutes or less to Alpaca's close"
    blocked = [{"symbol": r["symbol"], "reason": r["note"], "signal_at": r["signal_at"]}
               for r in rows if r["note"] and r["rules"] != 0 and r["target"] == 0 and not closing_time]
    room = Decimal(str(account.buying_power)) * BUYING_POWER_USE  # buying power left for new positions
    logger.info(json.dumps({"event": "daytrade_check", "trace_id": trace_id, "market_open": market_open,
                            "plan": {r["symbol"]: r["action"] for r in rows}, "day_change_pct": round(day_change_pct, 3)}))
    base_checks = ["paper venue", f"equity >= {MIN_EQUITY:,.0f}", f"day change > -{DAILY_LOSS_STOP_PCT}%",
                   "Day Trader market only", "no order already in flight", "schema-valid account, clock and positions",
                   f"drift {drift_state['tier']} (size x{drift_state['multiplier']})"]
    drift_note = {k: drift_state.get(k) for k in ("tier", "multiplier", "trades", "z", "payoff_vs_test", "slippage_vs_assumed")}

    def block(symbol: str, shares: int, why: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sealed = receipt(trace_id, symbol, "blocked", shares, {**(quote or {}), "equity": round(equity, 2),
                                                                 "drift": drift_note, "why": why}, base_checks)
        logger.info(json.dumps({"event": "order_blocked", "trace_id": trace_id, "symbol": symbol, "why": why,
                                "receipt": sealed["hash"]}))
        return {"symbol": symbol, "did": "open", "shares": shares, "status": "held back",
                "detail": f"{why}; it tries again on the next check", "receipt": sealed}

    actions: List[Dict[str, Any]] = []
    if market_open or body.stop:
        for row in rows:
            symbol, action = row["symbol"], row["action"]
            try:
                if action in ("close", "close_then_open"):
                    closed = _paper_exchange("DELETE", f"/v2/positions/{symbol}", keys)
                    actions.append({"symbol": symbol, "did": "close", "shares": -row["held"], "status": "sent",
                                    "order": _order_view(closed) if isinstance(closed, dict) else {},
                                    "receipt": receipt(trace_id, symbol, "close", -row["held"],
                                                       {"price": row["price"], "equity": round(equity, 2), "drift": drift_note,
                                                        "why": row["note"] or "the rules changed direction"}, base_checks)})
                    if action == "close_then_open":
                        for pause in FLAT_WAIT_SECONDS:  # Alpaca will not reverse a position while the close is pending
                            time.sleep(pause)
                            if _held(keys, [symbol]).get(symbol, 0) == 0:
                                break
                        else:
                            actions.append({"symbol": symbol, "did": "open", "shares": row["target"], "status": "next check",
                                            "detail": "the close had not filled yet; the new position opens on the next check"})
                            continue
                        action = "open"
                if action == "open":
                    quote = _spread(keys, symbol, str(plan.get("served_at") or ""))
                    if quote is None or quote["spread_pct"] > MAX_SPREAD_PCT:
                        why = ("no clean quote (none fresh on SIP or IEX)" if quote is None
                               else f"spread {quote['spread_pct']:.3f}% on {quote['feed']} is wider than {MAX_SPREAD_PCT}%")
                        actions.append(block(symbol, row["target"], why, quote))
                        continue
                    shares = row["target"]
                    fits = int((room / Decimal(str(row["price"]))).to_integral_value(rounding=ROUND_FLOOR)) if row["price"] > 0 else 0
                    if abs(shares) > fits:
                        shares = fits if shares > 0 else -fits
                    if shares == 0:
                        actions.append(block(symbol, row["target"], "not enough buying power left for this market", quote))
                        continue
                    room -= Decimal(abs(shares)) * Decimal(str(row["price"]))
                    checks = base_checks + [f"signal under {SIGNAL_TTL_SECONDS // 60} min old", f"spread <= {MAX_SPREAD_PCT}% ({quote['feed']})",
                                            "size within US$50,000 and 5,000 shares", "within 95% of buying power"]
                    order_id = client_order_id(symbol, row["signal_at"], shares, row["ref_price"])
                    sealed = receipt(trace_id, symbol, "open", shares,
                                     {**quote, "price": row["price"], "decision_price": row["ref_price"],
                                      "equity": round(equity, 2), "drift": drift_note}, checks)
                    actions.append({"symbol": symbol, "did": "open", "shares": shares, "status": "sent",
                                    "order": _order(keys, symbol, shares, order_id), "receipt": sealed})
                    logger.info(json.dumps({"event": "order_sent", "trace_id": trace_id, "symbol": symbol,
                                            "shares": shares, "client_order_id": order_id, "receipt": sealed["hash"]}))
            except HTTPException as error:
                if error.status_code == 401:
                    raise
                # A broker refusal (buying power, intraday margin, a halted stock) is reported and tried again on the
                # next check; it never stops the loop and never counts as bad keys.
                actions.append({"symbol": symbol, "did": action, "shares": row["target"], "status": "refused",
                                "detail": str(error.detail)})
                logger.warning(json.dumps({"event": "order_refused", "trace_id": trace_id, "symbol": symbol,
                                           "detail": str(error.detail)[:200]}))

    return {
        "ok": True, "paper": True, "checked_at": plan.get("served_at"), "plan_built_at": plan.get("generated_at"),
        "trace_id": trace_id, "policy": POLICY, "policy_hash": POLICY_HASH,
        "market_open": market_open, "next_open": clock.next_open, "next_close": clock.next_close,
        "equity": round(equity, 2), "day_change_pct": round(day_change_pct, 3),
        "loss_stop_pct": DAILY_LOSS_STOP_PCT, "stopped_for_loss": stopped_for_loss, "stopped": body.stop,
        "markets": rows, "actions": actions, "drift": drift_state, "blocked": blocked,
        "closing_time": closing_time, "minutes_to_close": None if to_close is None else round(to_close, 1),
        "buying_power": round(account.buying_power, 2),
        "record": {"unseen_instant": ((plan.get("record") or {}).get("book") or {}).get("test"),
                   "fill_speed": (plan.get("record") or {}).get("fill_speed")},
    }


@router.post("/daytrade", include_in_schema=False)
async def alpaca_daytrade(request: Request, body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    """One check of the Day Trader on this PAPER account: read the plan, send what is needed."""
    _require_gate(request)
    result: Dict[str, Any] = await run_in_threadpool(_check, body)
    return result

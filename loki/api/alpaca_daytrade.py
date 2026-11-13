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
* At least US$25,000 of equity: below that, US pattern-day-trader rules block
  repeated same-day round trips, and the rules would be cut off mid-day.
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
  snapshot, the risk policy and the checks it passed. The page adds each receipt
  to the device's signed, hash-chained trade log (engine/audit.js).

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
    "version": 1,
    "venue": "alpaca-paper",
    "min_equity_usd": 25_000,  # US pattern-day-trader threshold
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
    trading_blocked: bool = False
    account_blocked: bool = False

    @field_validator("equity", "last_equity", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    is_open: bool
    next_open: Optional[str] = None
    next_close: Optional[str] = None


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

    @field_validator("ap", "bp", mode="before")
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


def client_order_id(symbol: str, signal_at: Any, shares: int) -> str:
    """The same signal always gives the same id, so Alpaca refuses a second copy of an order."""
    stamp = "".join(ch for ch in str(signal_at or "unknown") if ch.isalnum())[:16]
    return f"loki-dt-{symbol}-{stamp}-{'buy' if shares > 0 else 'sell'}"


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
                     "action": action, "note": note, "signal_at": market.get("signal_at")})
    return rows


def _held(keys: AlpacaPaperKeys, symbols: List[str]) -> Dict[str, int]:
    raw = _paper_exchange("GET", "/v2/positions", keys)
    if not isinstance(raw, list):
        raise HTTPException(502, "Alpaca's positions answer was not the expected list.")
    positions = [_valid(AlpacaPosition, p, "position") for p in raw]
    return {p.symbol: signed_shares(p.model_dump()) for p in positions if p.symbol in symbols}


def _spread(keys: AlpacaPaperKeys, symbol: str) -> Optional[Dict[str, float]]:
    """The latest quote's bid, ask and spread (% of mid), or None when no clean quote is available."""
    try:
        raw = _exchange("GET", f"/v2/stocks/{symbol}/quotes/latest?feed=iex", keys, None, _DATA_BASE)
        quote = AlpacaQuote.model_validate((raw or {}).get("quote") if isinstance(raw, dict) else None)
    except (HTTPException, ValidationError):
        return None
    if quote.ap < quote.bp:
        return None
    mid = (quote.ap + quote.bp) / 2
    return {"bid": quote.bp, "ask": quote.ap, "spread_pct": round((quote.ap - quote.bp) / mid * 100, 4)}


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
        raise HTTPException(422, f"Day trading needs at least US${MIN_EQUITY:,.0f} in the account (US pattern-day-trader "
                                 f"rules block repeated same-day trades below that). This paper account has US${equity:,.2f}. "
                                 "You can reset a paper account to a larger balance in the Alpaca dashboard.")
    clock = _valid(AlpacaClock, _paper_exchange("GET", "/v2/clock", keys), "clock")
    market_open = clock.is_open

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
    stop = body.stop or stopped_for_loss

    raw_orders = _paper_exchange("GET", "/v2/orders?status=open&limit=100", keys)
    busy = {str(o.get("symbol")) for o in raw_orders if isinstance(o, dict)} if isinstance(raw_orders, list) else set()
    shortable: Dict[str, bool] = {}
    for market in markets:
        if wanted_side(market) < 0 and not stop:
            asset = _paper_exchange("GET", f"/v2/assets/{market['symbol']}", keys)
            shortable[str(market["symbol"])] = bool(isinstance(asset, dict) and asset.get("shortable") and asset.get("easy_to_borrow"))
    rows = plan_orders(markets, held, equity, float(plan.get("weight_per_market") or 0), busy, shortable, stop,
                       now=str(plan.get("served_at") or ""))
    logger.info(json.dumps({"event": "daytrade_check", "trace_id": trace_id, "market_open": market_open,
                            "plan": {r["symbol"]: r["action"] for r in rows}, "day_change_pct": round(day_change_pct, 3)}))
    base_checks = ["paper venue", f"equity >= {MIN_EQUITY:,.0f}", f"day change > -{DAILY_LOSS_STOP_PCT}%",
                   "Day Trader market only", "no order already in flight", "schema-valid account, clock and positions"]

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
                                                       {"price": row["price"], "equity": round(equity, 2)}, base_checks)})
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
                    quote = _spread(keys, symbol)
                    if quote is None or quote["spread_pct"] > MAX_SPREAD_PCT:
                        why = "no clean quote" if quote is None else f"spread {quote['spread_pct']:.3f}% is wider than {MAX_SPREAD_PCT}%"
                        actions.append({"symbol": symbol, "did": "open", "shares": row["target"], "status": "held back",
                                        "detail": f"{why}; it tries again on the next check"})
                        continue
                    checks = base_checks + [f"signal under {SIGNAL_TTL_SECONDS // 60} min old", f"spread <= {MAX_SPREAD_PCT}%",
                                            "size within US$50,000 and 5,000 shares"]
                    order_id = client_order_id(symbol, row["signal_at"], row["target"])
                    sealed = receipt(trace_id, symbol, "open", row["target"],
                                     {**quote, "price": row["price"], "equity": round(equity, 2)}, checks)
                    actions.append({"symbol": symbol, "did": "open", "shares": row["target"], "status": "sent",
                                    "order": _order(keys, symbol, row["target"], order_id), "receipt": sealed})
                    logger.info(json.dumps({"event": "order_sent", "trace_id": trace_id, "symbol": symbol,
                                            "shares": row["target"], "client_order_id": order_id, "receipt": sealed["hash"]}))
            except HTTPException as error:
                actions.append({"symbol": symbol, "did": action, "shares": row["target"], "status": "refused",
                                "detail": str(error.detail)})
                if error.status_code == 401:
                    raise

    return {
        "ok": True, "paper": True, "checked_at": plan.get("served_at"), "plan_built_at": plan.get("generated_at"),
        "trace_id": trace_id, "policy": POLICY, "policy_hash": POLICY_HASH,
        "market_open": market_open, "next_open": clock.next_open, "next_close": clock.next_close,
        "equity": round(equity, 2), "day_change_pct": round(day_change_pct, 3),
        "loss_stop_pct": DAILY_LOSS_STOP_PCT, "stopped_for_loss": stopped_for_loss, "stopped": body.stop,
        "markets": rows, "actions": actions,
        "record": {"unseen_instant": ((plan.get("record") or {}).get("book") or {}).get("test"),
                   "fill_speed": (plan.get("record") or {}).get("fill_speed")},
    }


@router.post("/daytrade", include_in_schema=False)
async def alpaca_daytrade(request: Request, body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    """One check of the Day Trader on this PAPER account: read the plan, send what is needed."""
    _require_gate(request)
    result: Dict[str, Any] = await run_in_threadpool(_check, body)
    return result

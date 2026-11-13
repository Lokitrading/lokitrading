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

Results are not guaranteed. The rules made money on the years they never saw,
but they lost money on most of the markets tested, and late fills cost a lot.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import ConfigDict

from loki.api.broker_alpaca import (
    AlpacaPaperKeys,
    _dict_payload,
    _number_or_none,
    _order_view,
    _paper_exchange,
    _require_gate,
)

router = APIRouter(prefix="/api/broker/alpaca", tags=["Broker relay"])

MIN_EQUITY = 25_000.0  # US pattern-day-trader threshold
DAILY_LOSS_STOP_PCT = 2.0  # close everything and stop for the day at this loss
MAX_PER_MARKET = 50_000.0  # US$ cap on one market's position
MAX_SHARES = 5_000
SIZE_BUFFER = 0.95  # leave room for the price moving between the check and the fill
FLAT_WAIT_SECONDS = (1.0, 1.5, 2.0)  # after closing, how long to wait for flat before reversing


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
    if side == 0 or price <= 0 or equity <= 0:
        return 0
    budget = min(equity * weight, MAX_PER_MARKET) * SIZE_BUFFER
    return side * min(int(math.floor(budget / price)), MAX_SHARES)


def plan_orders(markets: List[Dict[str, Any]], held: Dict[str, int], equity: float, weight: float,
                busy: set, shortable: Dict[str, bool], stop: bool) -> List[Dict[str, Any]]:
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
        now = held.get(symbol, 0)
        if symbol in busy:
            action, note = "wait", "an order for this market is still being filled"
        elif now == 0 and target == 0:
            action = "none"
        elif now == 0:
            action = "open"
        elif target == 0 or (now > 0) != (target > 0):
            action = "close_then_open" if target else "close"
        else:
            action = "hold"  # same direction: positions are not resized during the day
        rows.append({"symbol": symbol, "rules": side, "price": round(price, 4), "held": now, "target": target,
                     "action": action, "note": note, "signal_at": market.get("signal_at")})
    return rows


def _held(keys: AlpacaPaperKeys, symbols: List[str]) -> Dict[str, int]:
    raw = _paper_exchange("GET", "/v2/positions", keys)
    if not isinstance(raw, list):
        raise HTTPException(502, "Alpaca's positions answer was not the expected list.")
    return {str(p.get("symbol")): signed_shares(p) for p in raw if isinstance(p, dict) and str(p.get("symbol")) in symbols}


def _order(keys: AlpacaPaperKeys, symbol: str, shares: int) -> Dict[str, Any]:
    body = {"symbol": symbol, "qty": str(abs(shares)), "side": "buy" if shares > 0 else "sell",
            "type": "market", "time_in_force": "day", "extended_hours": False}
    return _order_view(_dict_payload(_paper_exchange("POST", "/v2/orders", keys, body), "order"))


def _check(body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    from loki.api import ai_paper

    keys = AlpacaPaperKeys(key_id=body.key_id, secret_key=body.secret_key)
    account = _dict_payload(_paper_exchange("GET", "/v2/account", keys), "account")
    if account.get("trading_blocked") or account.get("account_blocked"):
        raise HTTPException(422, "Alpaca has blocked trading on this paper account.")
    equity = _number_or_none(account.get("equity")) or 0.0
    last_equity = _number_or_none(account.get("last_equity")) or equity
    day_change_pct = (equity / last_equity - 1.0) * 100 if last_equity > 0 else 0.0
    if not body.stop and equity < MIN_EQUITY:
        raise HTTPException(422, f"Day trading needs at least US${MIN_EQUITY:,.0f} in the account (US pattern-day-trader "
                                 f"rules block repeated same-day trades below that). This paper account has US${equity:,.2f}. "
                                 "You can reset a paper account to a larger balance in the Alpaca dashboard.")
    clock = _dict_payload(_paper_exchange("GET", "/v2/clock", keys), "clock")
    market_open = bool(clock.get("is_open"))

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
    rows = plan_orders(markets, held, equity, float(plan.get("weight_per_market") or 0), busy, shortable, stop)

    actions: List[Dict[str, Any]] = []
    if market_open or body.stop:
        for row in rows:
            symbol, action = row["symbol"], row["action"]
            try:
                if action in ("close", "close_then_open"):
                    closed = _paper_exchange("DELETE", f"/v2/positions/{symbol}", keys)
                    actions.append({"symbol": symbol, "did": "close", "shares": -row["held"], "status": "sent",
                                    "order": _order_view(closed) if isinstance(closed, dict) else {}})
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
                    actions.append({"symbol": symbol, "did": "open", "shares": row["target"], "status": "sent",
                                    "order": _order(keys, symbol, row["target"])})
            except HTTPException as error:
                actions.append({"symbol": symbol, "did": action, "shares": row["target"], "status": "refused",
                                "detail": str(error.detail)})
                if error.status_code == 401:
                    raise

    return {
        "ok": True, "paper": True, "checked_at": plan.get("served_at"), "plan_built_at": plan.get("generated_at"),
        "market_open": market_open, "next_open": clock.get("next_open"), "next_close": clock.get("next_close"),
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

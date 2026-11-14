"""The memecoin bot on the visitor's own Alpaca PAPER account (crypto trades 24/7).

Each call is one check: read the memecoin rule's plan (``memecoin_api``), read the paper account,
and send the orders that make the account hold what the rule holds, within the allocation the
visitor chose. The browser calls it every 5 minutes while the Memecoins tab is open (the rule
decides on 4-hour bars). Nothing is stored here: the keys arrive with each call and go only to
Alpaca's paper endpoint.

The rule did NOT pass its own test (see ``loki_v8/memecoin_record.json``). This connector exists
to build a forward record with real paper fills, not because the rule is expected to make money.

Guards, checked on every call:

* Paper only: the host is pinned to ``paper-api.alpaca.markets``.
* Only DOGE, SHIB, PEPE, BONK, WIF and TRUMP are touched. At start, the account must not already
  hold any of them, so an existing position is never sold by mistake.
* The bot uses at most the allocation chosen (1-25% of equity, 10% by default), split by the rule's
  own weights, and never more than 95% of the cash Alpaca allows for crypto (its non-marginable
  buying power: crypto cannot be bought on margin).
* A coin is bought only when the rule turns it on and the account holds none, and sold in full when
  the rule turns it off. Positions are not resized between signals, so checks never fight.
* No buy while Alpaca's own quote spread is wider than 0.60% or older than 5 minutes.
* Orders: market, immediate-or-cancel, by dollar amount; each carries a fixed client order id built
  from the coin and the rule's bar, so a retried check cannot buy twice.
* Stop sells every bot coin at once and trades no more.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import quote as urlquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ConfigDict, Field

from loki.api.alpaca_daytrade import AlpacaAccount, _valid, _when
from loki.api.broker_alpaca import _DATA_BASE, AlpacaPaperKeys, _exchange, _order_view, _paper_exchange, _require_gate
from loki_v8 import memecoin as mc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/broker/alpaca", tags=["Broker relay"])

POLICY: Dict[str, Any] = {
    "version": 1,
    "venue": "alpaca-paper-crypto",
    "coins": mc.COINS,
    "allocation_pct": {"default": 10, "max": 25},
    "max_buying_power_used": "0.95",
    "max_spread_pct": 0.60,
    "quote_max_age_seconds": 300,
    "min_order_usd": 10,
    "order_type": "market, immediate-or-cancel, by dollar amount",
    "resizing": "none: buy when the rule turns on, sell in full when it turns off",
}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
MAX_SPREAD_PCT = float(POLICY["max_spread_pct"])
QUOTE_MAX_AGE = int(POLICY["quote_max_age_seconds"])
MIN_ORDER = Decimal(POLICY["min_order_usd"])
BUYING_POWER_USE = Decimal(POLICY["max_buying_power_used"])
CENT = Decimal("0.01")


class AlpacaMemecoinCheck(AlpacaPaperKeys):
    """One check of the memecoin bot on this paper account."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal["trade memecoins with paper money"]
    allocation_pct: float = Field(10.0, ge=1.0, le=25.0)
    start: bool = False
    stop: bool = False


def pair(coin: str) -> str:
    return f"{coin}/USD"


def _price(value: Any) -> Optional[float]:
    """A crypto price or size at full precision (memecoins trade far below a cent); junk is None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def crypto_room(raw_account: Any) -> Decimal:
    """Cash a crypto buy may use: Alpaca's non-marginable buying power (crypto cannot use margin)."""
    raw = raw_account if isinstance(raw_account, dict) else {}
    for field in ("non_marginable_buying_power", "cash"):
        value = _price(raw.get(field))
        if value is not None and value >= 0:
            return Decimal(str(value)) * BUYING_POWER_USE
    return Decimal(0)


def held_coins(positions: Any) -> Dict[str, float]:
    """Quantity held of each bot coin (Alpaca writes crypto positions as DOGEUSD or DOGE/USD)."""
    out: Dict[str, float] = {}
    for p in positions if isinstance(positions, list) else []:
        if not isinstance(p, dict):
            continue
        symbol = str(p.get("symbol") or "").replace("/", "").upper()
        coin = symbol[:-3] if symbol.endswith("USD") else ""
        qty = _price(p.get("qty"))
        if coin in mc.COINS and qty:
            out[coin] = qty
    return out


def target_usd(weight: float, equity: float, allocation_pct: float) -> Decimal:
    """Dollars for one coin: the rule's weight of the allocated slice of equity, cut to the cent."""
    try:
        amount = Decimal(str(weight)) * Decimal(str(equity)) * Decimal(str(allocation_pct)) / 100
    except ArithmeticError:
        return Decimal(0)
    return amount.quantize(CENT, rounding=ROUND_FLOOR) if amount.is_finite() and amount > 0 else Decimal(0)


def plan_actions(holdings: List[Dict[str, Any]], held: Dict[str, float], busy: set, equity: float,
                 allocation_pct: float, stop: bool) -> List[Dict[str, Any]]:
    """What to do for each coin. Pure: no network."""
    rows = []
    for h in holdings:
        coin = h["coin"]
        want = bool(h.get("holding")) and not stop
        have = held.get(coin, 0.0) > 0
        if coin in busy:
            action = "wait"
        elif have and not want:
            action = "sell"
        elif want and not have:
            action = "buy"
        else:
            action = "hold" if have else "none"
        rows.append({"coin": coin, "rule": "hold" if h.get("holding") else "cash", "held_qty": held.get(coin, 0.0), "action": action,
                     "target_usd": float(target_usd(h.get("weight", 0.0), equity, allocation_pct)) if want else 0.0,
                     "as_of": h.get("as_of")})
    return rows


def order_id(coin: str, as_of: Any) -> str:
    stamp = "".join(ch for ch in str(as_of or "unknown") if ch.isalnum())[:14]
    return f"loki-mc-{coin}-{stamp}-buy"


def _quote(keys: AlpacaPaperKeys, coins: List[str], now: Optional[datetime]) -> Dict[str, Tuple[Optional[Dict[str, Any]], str]]:
    """Alpaca's own latest crypto quote for each coin, or why it is not fit to buy on."""
    out: Dict[str, Tuple[Optional[Dict[str, Any]], str]] = {c: (None, "no quote from Alpaca") for c in coins}
    try:
        raw = _exchange("GET", "/v1beta3/crypto/us/latest/quotes?symbols=" + urlquote(",".join(pair(c) for c in coins), safe=","),
                        keys, None, _DATA_BASE)
    except HTTPException as error:
        if error.status_code == 401:
            raise
        return out
    quotes = raw.get("quotes") if isinstance(raw, dict) else None
    for coin in coins:
        q = quotes.get(pair(coin)) if isinstance(quotes, dict) else None
        bid, ask = _price((q or {}).get("bp")), _price((q or {}).get("ap"))
        stamp = _when((q or {}).get("t"))
        if not bid or not ask or ask <= bid:
            out[coin] = (None, "no two-sided quote from Alpaca")
            continue
        age = (now - stamp).total_seconds() if now and stamp else None
        if age is None or age > QUOTE_MAX_AGE:
            out[coin] = (None, "Alpaca's quote is stale")
            continue
        mid = (bid + ask) / 2
        out[coin] = ({"bid": bid, "ask": ask, "spread_pct": round((ask - bid) / mid * 100, 3), "quote_age_s": round(age, 1)}, "")
    return out


def _receipt(trace: str, coin: str, action: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    body = {"trace_id": trace, "coin": coin, "action": action, "policy": POLICY_HASH, "snapshot": snapshot,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {**body, "hash": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()}


def _check(body: AlpacaMemecoinCheck) -> Dict[str, Any]:
    from loki.api import memecoin_api

    trace = uuid.uuid4().hex[:16]
    keys = AlpacaPaperKeys(key_id=body.key_id, secret_key=body.secret_key)
    raw_account = _paper_exchange("GET", "/v2/account", keys)
    account = _valid(AlpacaAccount, raw_account, "account")
    if account.trading_blocked or account.account_blocked:
        raise HTTPException(422, "Alpaca has blocked trading on this paper account.")
    clock = _paper_exchange("GET", "/v2/clock", keys)
    now = _when(clock.get("timestamp")) if isinstance(clock, dict) else None
    held = held_coins(_paper_exchange("GET", "/v2/positions", keys))
    if body.start and held:
        listed = ", ".join(sorted(held))
        raise HTTPException(409, f"This paper account already holds {listed}. The memecoin bot trades those coins itself and would sell "
                                 "them. Sell them first, or use a separate paper account.")
    plan = memecoin_api._cached_plan()
    raw_orders = _paper_exchange("GET", "/v2/orders?status=open&limit=200", keys)
    busy = {str(o.get("symbol") or "").replace("/", "")[:-3] for o in raw_orders if isinstance(o, dict)} if isinstance(raw_orders, list) else set()
    rows = plan_actions(plan["holdings"], held, busy, account.equity, body.allocation_pct, body.stop)
    buying = [r["coin"] for r in rows if r["action"] == "buy"]
    quotes = _quote(keys, buying, now) if buying else {}
    room = crypto_room(raw_account)
    actions: List[Dict[str, Any]] = []
    for row in rows:
        coin, action = row["coin"], row["action"]
        try:
            if action == "sell":
                closed = _paper_exchange("DELETE", f"/v2/positions/{coin}USD", keys)
                actions.append({"coin": coin, "did": "sell", "status": "sent", "order": _order_view(closed) if isinstance(closed, dict) else {},
                                "receipt": _receipt(trace, coin, "sell", {"why": "stop" if body.stop else "the rule turned it off"})})
            elif action == "buy":
                quote, why = quotes.get(coin) or (None, "no quote from Alpaca")
                amount = min(Decimal(str(row["target_usd"])), room).quantize(CENT, rounding=ROUND_FLOOR)
                if quote is None or quote["spread_pct"] > MAX_SPREAD_PCT:
                    why = why or f"spread {quote['spread_pct']:.2f}% is wider than {MAX_SPREAD_PCT}%"  # type: ignore[index]
                elif amount < MIN_ORDER:
                    why = f"US${amount} is below the US${MIN_ORDER} minimum this bot sends"
                else:
                    why = ""
                if why:
                    actions.append({"coin": coin, "did": "buy", "status": "held back", "detail": f"{why}; it tries again on the next check",
                                    "receipt": _receipt(trace, coin, "blocked", {**(quote or {}), "why": why})})
                    continue
                room -= amount
                order = {"symbol": pair(coin), "notional": str(amount), "side": "buy", "type": "market", "time_in_force": "ioc",
                         "client_order_id": order_id(coin, row["as_of"])}
                try:
                    sent = _paper_exchange("POST", "/v2/orders", keys, order)
                    view = _order_view(sent) if isinstance(sent, dict) else {}
                except HTTPException as error:
                    if error.status_code == 422 and "client_order_id" in str(error.detail):
                        view = {"status": "already sent"}  # a retried check: Alpaca has this order
                    else:
                        raise
                actions.append({"coin": coin, "did": "buy", "status": "sent", "usd": float(amount), "order": view,
                                "receipt": _receipt(trace, coin, "buy", {**(quote or {}), "usd": float(amount), "allocation_pct": body.allocation_pct})})
        except HTTPException as error:
            if error.status_code == 401:
                raise
            actions.append({"coin": coin, "did": action, "status": "refused", "detail": str(error.detail)})
            logger.warning(json.dumps({"event": "memecoin_order_refused", "trace_id": trace, "coin": coin, "detail": str(error.detail)[:200]}))
    logger.info(json.dumps({"event": "memecoin_check", "trace_id": trace, "plan": {r["coin"]: r["action"] for r in rows}}))
    return {"ok": True, "paper": True, "trace_id": trace, "policy": POLICY, "policy_hash": POLICY_HASH,
            "equity": round(account.equity, 2), "crypto_buying_power": float(crypto_room(raw_account) / BUYING_POWER_USE),
            "allocation_pct": body.allocation_pct,
            "coins": rows, "actions": actions, "stopped": body.stop, "rule": plan.get("label"), "checked_at": now.isoformat() if now else None}


@router.post("/memecoin", include_in_schema=False)
async def alpaca_memecoin(request: Request, body: AlpacaMemecoinCheck) -> Dict[str, Any]:
    """One check of the memecoin bot on this PAPER account: read the plan, send what is needed."""
    _require_gate(request)
    result: Dict[str, Any] = await run_in_threadpool(_check, body)
    return result

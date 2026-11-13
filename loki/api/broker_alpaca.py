"""Alpaca paper-account relay: key validation, account facts, explicit orders.

The browser holds the paper-trading keys (they never persist server-side) and
hands them to this relay per request. The relay talks ONLY to Alpaca's PAPER
endpoint. Reading is account facts; writing is orders the user clicked, one
place-order call per request, day-only, regular hours, hard relay caps
(100 shares a ticket, limit price ceiling) so a stray click can type neither
a fortune nor a flood. Error answers are honest: a rejection says the keys
were rejected, a refused order says Alpaca refused it, and an unreachable
Alpaca never pretends otherwise.
"""
from __future__ import annotations

import json
import math
import urllib.error
from datetime import datetime, timedelta, timezone
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

import loki.api.site_access as site_access

router = APIRouter(prefix="/api/broker/alpaca", tags=["Broker relay"])

_PAPER_BASE = "https://paper-api.alpaca.markets"
_DATA_BASE = "https://data.alpaca.markets"
_RELAY_UA = "Mozilla/5.0 (compatible; LokiWorkspace/2.0; Alpaca paper relay)"
# Relay-side guards for the order rail. They are not advice and not a neutral
# opinion about sizing — they exist so the relay cannot be a fire hose.
ORDER_QTY_MAX = 100
LIMIT_PRICE_MAX = 10_000.0
BARS_MAX = 60

_ORDER_FIELDS = (
    "id", "symbol", "qty", "side", "type", "time_in_force", "status",
    "created_at", "submitted_at", "filled_qty", "filled_avg_price",
)


class AlpacaPaperKeys(BaseModel):
    """Paper-trading API keys as handed in by the browser, never stored."""

    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(min_length=10, max_length=32, pattern=r"^[A-Za-z0-9]{10,32}$")
    secret_key: str = Field(min_length=32, max_length=96, pattern=r"^[A-Za-z0-9_+\-/=]{32,96}$")


class AlpacaOrder(AlpacaPaperKeys):
    """One explicit paper order, exactly as the user shaped it on screen."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=10, pattern=r"^[A-Z][A-Z0-9.]{0,9}$")
    side: Literal["buy", "sell"]
    quantity: int = Field(ge=1, le=ORDER_QTY_MAX)
    order_type: Literal["market", "limit"]
    limit_price: Optional[float] = Field(default=None, ge=0.01, le=LIMIT_PRICE_MAX)


class AlpacaOrderRef(AlpacaPaperKeys):
    """One existing paper order, by Alpaca's id."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AlpacaPositionRef(AlpacaPaperKeys):
    """One held symbol (stocks as 'AAPL', crypto as 'BTCUSD' or 'BTC/USD')."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z][A-Z0-9.]{0,9}(/USD)?$")


class AlpacaApply(AlpacaPaperKeys):
    """Apply the Portfolio AI's plan: an explicit, typed confirmation is required."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal["place paper orders"]


class AlpacaBarsRequest(AlpacaPaperKeys):
    """A read-only bars request for the autopilot's published rule."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z][A-Za-z0-9.]{0,9}$")
    minutes: int = Field(default=25, ge=12, le=60)


def _require_gate(request: Request) -> None:
    """The relay serves people who already passed the workspace's door."""
    if not site_access.gate_enabled():
        # No door on this deployment (local run): nothing to have passed.
        return
    cookie = request.cookies.get(site_access.GATE_COOKIE)
    if not site_access.verify_gate_cookie_value(cookie or ""):
        raise HTTPException(403, "Open the workspace through its access door first.")


def _alpaca_error_detail(error: urllib.error.HTTPError) -> str:
    """Alpaca explains refusals in JSON; pass the human part through, clipped."""
    snippet = ""
    try:
        body = error.read()
        if body:
            parsed = json.loads(body.decode())
            if isinstance(parsed, dict):
                snippet = str(parsed.get("message") or parsed.get("detail") or "")
    except Exception:  # noqa: BLE001 - the detail must never become a crash
        snippet = ""
    if not snippet:
        snippet = str(error.reason or f"HTTP {error.code}")
    return snippet[:200]


def _paper_exchange(
    method: str, path: str, keys: AlpacaPaperKeys, body: Optional[Dict[str, Any]] = None
) -> Any:
    """One credentialed round-trip to Alpaca's PAPER endpoint, honest errors."""
    return _exchange(method, path, keys, body, _PAPER_BASE)


def _exchange(
    method: str, path: str, keys: AlpacaPaperKeys, body: Optional[Dict[str, Any]] = None,
    base: str = _PAPER_BASE,
) -> Any:
    """One credentialed round-trip to a pinned Alpaca host, honest errors."""
    data = json.dumps(body).encode() if body is not None else None
    headers: Dict[str, str] = {
        "User-Agent": _RELAY_UA,
        "APCA-API-KEY-ID": keys.key_id,
        "APCA-API-SECRET-KEY": keys.secret_key,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310 - fixed https URLs only
        f"{base}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as res:
            raw = res.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise HTTPException(401, "Alpaca rejected those keys — copy them again from the Alpaca dashboard (paper keys).") from error
        if error.code == 403:
            # Alpaca's 403 is a refusal of this request (not enough buying power, an intraday margin
            # deficit, a pattern-day-trader block, a data plan without SIP), not a sign of bad keys.
            raise HTTPException(422, f"Alpaca refused this (403): {_alpaca_error_detail(error)}") from error
        if error.code in (400, 422):
            raise HTTPException(422, f"Alpaca refused this order: {_alpaca_error_detail(error)}") from error
        if error.code == 429:
            raise HTTPException(503, "Alpaca is rate-limiting this account; wait a minute and check again.") from error
        if error.code == 404:
            raise HTTPException(502, "Alpaca's paper endpoint answered 404; its API may have moved.") from error
        raise HTTPException(502, f"Alpaca answered HTTP {error.code}; try again shortly.") from error
    except (urllib.error.URLError, OSError) as error:
        raise HTTPException(503, "Alpaca is unreachable right now; try again shortly.") from error
    if not raw.strip():
        return None  # 204 No Content (e.g. a cancelled order)
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise HTTPException(502, "Alpaca sent something this relay could not parse.") from error
    return payload


def _number_or_none(value: Any) -> Optional[float]:
    """Round money numbers to cents; junk becomes None, never 0 (no invention)."""
    try:
        number = float(value)
        return round(number, 2) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _account_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the fields that leave the relay; nothing else passes through."""
    account_number = str(payload.get("account_number") or "")
    return {
        "paper": True,
        "status": str(payload.get("status") or "unknown"),
        "account_number": account_number[-6:] or None,
        "currency": str(payload.get("currency") or "USD"),
        "cash": _number_or_none(payload.get("cash")),
        "equity": _number_or_none(payload.get("equity")),
        "buying_power": _number_or_none(payload.get("buying_power")),
        "trading_blocked": bool(payload.get("trading_blocked", False)),
        "account_blocked": bool(payload.get("account_blocked", False)),
    }


def _order_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the order fields that leave the relay; status stays honest."""
    view: Dict[str, Any] = {}
    for field in _ORDER_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if field == "qty":
            quantity = _int_or_none(value)
            if quantity is None:
                continue
            value = quantity
        if field == "filled_avg_price":
            value = _number_or_none(value)
            if value is None:
                continue
        view[field] = str(value) if not isinstance(value, (int, float)) else value
    return view


_POSITION_FIELDS = (
    "symbol", "qty", "side", "asset_class", "status",
    "avg_entry_price", "avg_cost", "current_price",
    "market_value", "cost_basis", "unrealized_pl", "unrealized_plpc",
)

_POSITION_MONEY = frozenset(
    {"avg_entry_price", "avg_cost", "current_price",
     "market_value", "cost_basis", "unrealized_pl", "unrealized_plpc"}
)


def _position_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the portfolio fields that leave the relay; holdings stay honest."""
    view: Dict[str, Any] = {}
    for field in _POSITION_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if field == "unrealized_plpc":
            try:
                value = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(value):
                continue
        elif field in _POSITION_MONEY:
            number = _number_or_none(value)
            if number is None:
                continue
            value = number
        view[field] = str(value) if not isinstance(value, (int, float)) else value
    return view


def _dict_payload(payload: Any, what: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(502, f"Alpaca's answer was not the expected {what} payload.")
    return payload


@router.post("/test", include_in_schema=False)
async def alpaca_test_keys(request: Request, keys: AlpacaPaperKeys) -> Dict[str, Any]:
    _require_gate(request)
    payload = await run_in_threadpool(_paper_exchange, "GET", "/v2/account", keys)
    return {"ok": True, "account_status": str(_dict_payload(payload, "account").get("status") or "unknown"), "paper": True}


@router.post("/account", include_in_schema=False)
async def alpaca_account(request: Request, keys: AlpacaPaperKeys) -> Dict[str, Any]:
    _require_gate(request)
    payload = await run_in_threadpool(_paper_exchange, "GET", "/v2/account", keys)
    return _account_view(_dict_payload(payload, "account"))


@router.post("/order", include_in_schema=False)
async def alpaca_place_order(request: Request, order: AlpacaOrder) -> Dict[str, Any]:
    _require_gate(request)
    if order.order_type == "limit" and order.limit_price is None:
        raise HTTPException(422, "A limit order needs a limit price before it can travel.")
    if order.order_type == "market" and order.limit_price is not None:
        raise HTTPException(422, "A market order takes no limit price — send it as a limit order instead.")
    body: Dict[str, Any] = {
        "symbol": order.symbol,
        "qty": str(order.quantity),
        "side": order.side,
        "type": order.order_type,
        "time_in_force": "day",
        "extended_hours": False,
    }
    if order.order_type == "limit":
        body["limit_price"] = f"{order.limit_price:.2f}"
    payload = await run_in_threadpool(
        _paper_exchange, "POST", "/v2/orders", order, body
    )
    return {"ok": True, "paper": True, "order": _order_view(_dict_payload(payload, "order"))}


@router.post("/orders", include_in_schema=False)
async def alpaca_recent_orders(request: Request, keys: AlpacaPaperKeys) -> Dict[str, Any]:
    """The paper book's recent orders — the log the panel reads, whitelisted."""
    _require_gate(request)
    payload = await run_in_threadpool(
        _paper_exchange, "GET", "/v2/orders?status=all&limit=12&direction=desc", keys
    )
    if not isinstance(payload, list):
        raise HTTPException(502, "Alpaca's order log was not the expected list.")
    orders: List[Dict[str, Any]] = [_order_view(order) for order in payload if isinstance(order, dict)]
    orders.sort(key=lambda view: str(view.get("created_at") or ""), reverse=True)
    return {"ok": True, "paper": True, "orders": orders}


@router.post("/positions", include_in_schema=False)
async def alpaca_positions(request: Request, keys: AlpacaPaperKeys) -> Dict[str, Any]:
    """What the paper account actually holds right now — read-only, whitelisted.

    The portfolio panel renders exactly this list. Empty means no positions,
    never an invented holding; a failed read surfaces an honest error.
    """
    _require_gate(request)
    payload = await run_in_threadpool(_paper_exchange, "GET", "/v2/positions", keys)
    if not isinstance(payload, list):
        raise HTTPException(502, "Alpaca's positions answer was not the expected list.")
    positions = [_position_view(item) for item in payload if isinstance(item, dict)]
    return {"ok": True, "paper": True, "positions": positions}


@router.post("/bars", include_in_schema=False)
async def alpaca_bars(request: Request, bars: AlpacaBarsRequest) -> Dict[str, Any]:
    """One-minute bars from Alpaca's data feed (the free IEX feed), whitelisted.

    Read-only against data.alpaca.markets; the panel's published rule eats the
    closes. No signals are computed here — the relay just fetches facts.
    """
    _require_gate(request)
    symbol = bars.symbol.upper()
    # Without a start Alpaca only searches today, so evenings and weekends came
    # back empty ("bars": null) and the panel showed an error. Look back a week
    # and take the newest bars: outside market hours that is the last session.
    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/v2/stocks/{symbol}/bars?timeframe=1Min&limit={bars.minutes}&feed=iex&sort=desc&start={start}"
    payload = await run_in_threadpool(_exchange, "GET", path, bars, None, _DATA_BASE)
    if not isinstance(payload, dict) or not (payload.get("bars") is None or isinstance(payload.get("bars"), list)):
        raise HTTPException(502, "Alpaca's bar feed arrived in an unexpected shape.")
    views: List[Dict[str, Any]] = []
    raw_bars = [bar for bar in (payload.get("bars") or []) if isinstance(bar, dict)]
    raw_bars.sort(key=lambda bar: str(bar.get("t") or ""))  # oldest first, whatever order Alpaca used
    for bar in raw_bars[-BARS_MAX:]:
        if not isinstance(bar, dict):
            continue
        timestamp = str(bar.get("t") or "")
        close = _number_or_none(bar.get("c"))
        if not timestamp or close is None:
            continue
        views.append({
            "t": timestamp,
            "o": _number_or_none(bar.get("o")),
            "h": _number_or_none(bar.get("h")),
            "l": _number_or_none(bar.get("l")),
            "c": close,
            "v": _number_or_none(bar.get("v")),
        })
    return {"ok": True, "paper": True, "symbol": symbol, "bars": views}


@router.post("/cancel", include_in_schema=False)
async def alpaca_cancel_order(request: Request, ref: AlpacaOrderRef) -> Dict[str, Any]:
    """Cancel one open paper order. Already-filled orders cannot be cancelled."""
    _require_gate(request)
    await run_in_threadpool(_paper_exchange, "DELETE", f"/v2/orders/{ref.order_id}", ref)
    return {"ok": True, "paper": True, "cancelled": ref.order_id}


@router.post("/close", include_in_schema=False)
async def alpaca_close_position(request: Request, ref: AlpacaPositionRef) -> Dict[str, Any]:
    """Close one whole paper position at market (the order Alpaca creates is returned)."""
    _require_gate(request)
    symbol = ref.symbol.replace("/", "")
    payload = await run_in_threadpool(_paper_exchange, "DELETE", f"/v2/positions/{symbol}", ref)
    order = _order_view(payload) if isinstance(payload, dict) else {}
    return {"ok": True, "paper": True, "order": order}


# --- Let the Portfolio AI manage this paper account ---------------------------
AI_MAX_ORDERS = 25  # the AI trades at most 20 markets; anything more is a bug, not a plan


def _ai_context(keys: AlpacaPaperKeys) -> Dict[str, Any]:
    from loki.api import ai_paper, alpaca_ai

    account = _dict_payload(_paper_exchange("GET", "/v2/account", keys), "account")
    clock = _dict_payload(_paper_exchange("GET", "/v2/clock", keys), "clock")
    raw_positions = _paper_exchange("GET", "/v2/positions", keys)
    if not isinstance(raw_positions, list):
        raise HTTPException(502, "Alpaca's positions answer was not the expected list.")
    held = {str(p.get("symbol")): p for p in raw_positions if isinstance(p, dict)}
    equity = _number_or_none(account.get("equity")) or 0.0
    if equity <= 0:
        raise HTTPException(422, "This paper account has no equity to allocate.")
    plan = ai_paper.ai_portfolio(Response())
    trades, notes = alpaca_ai.plan_trades(plan, equity, held, bool(clock.get("is_open")))
    return {"equity": equity, "stocks_open": bool(clock.get("is_open")), "plan": plan, "trades": trades, "notes": notes}


def _plan_view(ctx: Dict[str, Any]) -> Dict[str, Any]:
    plan = ctx["plan"]
    return {
        "ok": True, "paper": True, "equity": ctx["equity"], "us_market_open": ctx["stocks_open"],
        "plan_as_of": plan.get("as_of"), "ai_invested_pct": round(float(plan.get("gross") or 0) * 100, 1),
        "targets": [{"symbol": r["symbol"], "weight_pct": round(float(r["weight"]) * 100, 2)}
                    for r in plan.get("positions", []) if float(r.get("weight") or 0) > 0]
                   + [{"symbol": p["symbol"], "weight_pct": round(float(p["weight"]) * 100, 2), "cash_parking": True}
                      for p in [plan.get("cash_parking") or {}] if float(p.get("weight") or 0) > 0],
        "trades": ctx["trades"], "notes": ctx["notes"],
    }


@router.post("/ai-preview", include_in_schema=False)
async def alpaca_ai_preview(request: Request, keys: AlpacaPaperKeys) -> Dict[str, Any]:
    """What the Portfolio AI would trade in this paper account right now. Places nothing."""
    _require_gate(request)
    return _plan_view(await run_in_threadpool(_ai_context, keys))


@router.post("/ai-apply", include_in_schema=False)
async def alpaca_ai_apply(request: Request, body: AlpacaApply) -> Dict[str, Any]:
    """Place the Portfolio AI's rebalancing orders in this PAPER account.

    The plan is recomputed here (never trusted from the browser), sells go
    before buys, and at most AI_MAX_ORDERS orders are sent.
    """
    _require_gate(request)
    ctx = await run_in_threadpool(_ai_context, body)
    keys = AlpacaPaperKeys(key_id=body.key_id, secret_key=body.secret_key)
    results: List[Dict[str, Any]] = []
    for trade in ctx["trades"][:AI_MAX_ORDERS]:
        try:
            if trade["side"] == "close":
                payload = await run_in_threadpool(
                    _paper_exchange, "DELETE", "/v2/positions/" + trade["symbol"].replace("/", ""), keys)
            else:
                order: Dict[str, Any] = {"symbol": trade["symbol"], "side": trade["side"], "type": "market",
                                         "time_in_force": "gtc" if trade["crypto"] else "day"}
                if trade["side"] == "buy":
                    order["notional"] = f"{trade['notional']:.2f}"
                else:
                    order["qty"] = f"{trade['qty']:.9f}".rstrip("0").rstrip(".")
                payload = await run_in_threadpool(_paper_exchange, "POST", "/v2/orders", keys, order)
            results.append({**trade, "status": "placed",
                            "order": _order_view(payload) if isinstance(payload, dict) else {}})
        except HTTPException as error:
            results.append({**trade, "status": "failed", "detail": str(error.detail)})
    view = _plan_view(ctx)
    view["trades"] = results
    view["placed"] = sum(1 for r in results if r["status"] == "placed")
    return view

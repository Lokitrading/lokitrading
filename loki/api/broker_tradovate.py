"""Stateless Tradovate relay: the browser holds the token, Loki stores nothing.

Tradovate's API sends no CORS headers, so a browser cannot call it directly.
This relay forwards exactly one call per request to Tradovate's fixed official
hosts, with the visitor's own access token (or, once, their API credentials to
obtain one). Nothing is written to disk, a database or a cookie. Orders are
the ones the visitor confirmed on screen; live orders additionally need the
literal confirmation phrase, and the relay caps contracts per ticket.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, SecretStr

import loki.api.site_access as site_access
from core import tradovate

router = APIRouter(prefix="/api/broker/tradovate", tags=["Broker relay"])

MAX_CONTRACTS = 10
LIVE_PHRASE = "trade real money"
_TOKEN = re.compile(r"^[A-Za-z0-9._\-]{16,4096}$")
_ORDER_STATUS = ("id", "accountId", "contractId", "timestamp", "action", "ordStatus", "orderType", "price", "stopPrice")
Env = Literal["demo", "live"]


def _gate(request: Request) -> None:
    if site_access.gate_enabled() and not site_access.verify_gate_cookie_value(request.cookies.get(site_access.GATE_COOKIE)):
        raise HTTPException(401, "Open Loki through the access page first.")


def _token(authorization: Optional[str]) -> str:
    raw = (authorization or "").removeprefix("Bearer ").strip()
    if not _TOKEN.match(raw):
        raise HTTPException(401, "Connect Tradovate first: no valid access token was sent.")
    return raw


def _call(method: str, environment: str, token: str, path: str, body: Optional[Dict[str, Any]] = None,
          params: Optional[Dict[str, Any]] = None) -> Any:
    if environment not in tradovate.HOSTS:
        raise HTTPException(422, "Pick demo or live.")
    try:
        res = requests.request(method, f"{tradovate.HOSTS[environment]}/{path}", json=body, params=params,
                               headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                               timeout=(4, 10), allow_redirects=False)
    except requests.RequestException:
        raise HTTPException(503, "Tradovate is unreachable right now; nothing was sent twice.") from None
    if res.status_code in (401, 403):
        raise HTTPException(401, "Tradovate rejected the token. Reconnect (tokens last about 90 minutes).")
    if res.status_code == 429:
        raise HTTPException(429, "Tradovate is rate limiting; wait a minute.")
    if res.status_code >= 400:
        raise HTTPException(502, f"Tradovate answered HTTP {res.status_code}.")
    try:
        return res.json()
    except ValueError:
        raise HTTPException(502, "Tradovate answered with something that is not JSON.") from None


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Env
    name: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=200)
    cid: int = Field(ge=1, le=10_000_000)
    sec: SecretStr = Field(min_length=8, max_length=200)
    app_id: str = Field(default="Loki", min_length=1, max_length=40, pattern=r"^[A-Za-z0-9 ._-]+$")


@router.post("/token")
async def token(request: Request, body: TokenRequest, response: Response) -> Dict[str, Any]:
    """Exchange API credentials for a token. The credentials are forwarded once and dropped."""
    _gate(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        access, expires = await run_in_threadpool(
            tradovate.request_token, body.environment, body.name, body.password.get_secret_value(),
            body.cid, body.sec.get_secret_value(), body.app_id)
    except tradovate.TradovateError as exc:
        raise HTTPException(401, str(exc)) from None
    return {"environment": body.environment, "access_token": access, "expires": expires,
            "note": "Kept only in this browser tab. Loki never stores Tradovate credentials or tokens."}


@router.get("/{environment}/snapshot")
async def snapshot(environment: Env, request: Request, response: Response,
                   authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _gate(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        data: Dict[str, Any] = await run_in_threadpool(tradovate.snapshot, environment, _token(authorization))
    except tradovate.TradovateError as exc:
        raise HTTPException(502, str(exc)) from None
    try:
        orders = await run_in_threadpool(_call, "GET", environment, _token(authorization), "order/list")
        data["orders"] = [{k: o.get(k) for k in _ORDER_STATUS} for o in (orders or [])[-50:] if isinstance(o, dict)]
    except HTTPException as exc:
        data["messages"].append(str(exc.detail))
    data["execution_enabled"] = True
    return data


@router.get("/{environment}/contract")
async def contract(environment: Env, request: Request,
                   name: str = Query(min_length=2, max_length=12, pattern=r"^[A-Z0-9]+$"),
                   authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _gate(request)
    found = await run_in_threadpool(_call, "GET", environment, _token(authorization), "contract/find", None, {"name": name})
    if not isinstance(found, dict) or not found.get("id"):
        raise HTTPException(404, f"Tradovate has no contract called {name}.")
    return {"id": found.get("id"), "name": found.get("name")}


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    account_spec: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9 ._\-]+$")
    symbol: str = Field(min_length=2, max_length=12, pattern=r"^[A-Z0-9]+$")
    action: Literal["Buy", "Sell"]
    quantity: int = Field(ge=1, le=MAX_CONTRACTS)
    order_type: Literal["Market", "Limit", "Stop"]
    price: Optional[float] = Field(default=None, gt=0, lt=10_000_000)
    confirm: str = Field(default="", max_length=40)


@router.post("/{environment}/order")
async def place_order(environment: Env, body: OrderBody, request: Request, response: Response,
                      authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """One confirmed order. Live needs the literal phrase; every order is marked automated-assist."""
    _gate(request)
    response.headers["Cache-Control"] = "no-store"
    if environment == "live" and body.confirm.strip().lower() != LIVE_PHRASE:
        raise HTTPException(428, f'Live orders need the confirmation phrase "{LIVE_PHRASE}".')
    if body.order_type != "Market" and body.price is None:
        raise HTTPException(422, f"{body.order_type} orders need a price.")
    payload: Dict[str, Any] = {"accountSpec": body.account_spec, "accountId": body.account_id, "action": body.action,
                               "symbol": body.symbol, "orderQty": body.quantity, "orderType": body.order_type,
                               "timeInForce": "Day", "isAutomated": True}
    if body.order_type == "Limit":
        payload["price"] = body.price
    elif body.order_type == "Stop":
        payload["stopPrice"] = body.price
    answer = await run_in_threadpool(_call, "POST", environment, _token(authorization), "order/placeorder", payload)
    if not isinstance(answer, dict):
        raise HTTPException(502, "Tradovate answered the order unexpectedly.")
    if answer.get("failureReason") or answer.get("failureText"):
        raise HTTPException(422, f"Tradovate refused the order: {str(answer.get('failureText') or answer.get('failureReason'))[:200]}")
    return {"environment": environment, "order_id": answer.get("orderId"), "sent": payload}


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


@router.post("/{environment}/cancel")
async def cancel(environment: Env, body: CancelBody, request: Request,
                 authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _gate(request)
    answer = await run_in_threadpool(_call, "POST", environment, _token(authorization), "order/cancelorder", {"orderId": body.order_id})
    return {"cancelled": body.order_id, "answer": answer if isinstance(answer, dict) else None}


class FlattenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    contract_ids: List[int] = Field(min_length=1, max_length=50)


@router.post("/{environment}/flatten")
async def flatten(environment: Env, body: FlattenBody, request: Request,
                  authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Kill switch: liquidate each listed position (Tradovate also cancels its working orders)."""
    _gate(request)
    token_value = _token(authorization)
    results: List[Dict[str, Any]] = []
    for contract_id in body.contract_ids:
        try:
            answer = await run_in_threadpool(_call, "POST", environment, token_value, "order/liquidateposition",
                                             {"accountId": body.account_id, "contractId": contract_id, "admin": False})
            results.append({"contract_id": contract_id, "ok": True, "answer": answer if isinstance(answer, dict) else None})
        except HTTPException as exc:
            results.append({"contract_id": contract_id, "ok": False, "error": str(exc.detail)})
    return {"environment": environment, "results": results}

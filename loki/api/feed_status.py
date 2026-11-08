"""Live health of every upstream the workspace depends on (the top-bar status lights).

Each check is a small real request (a quote, one odds row, the CLOB clock),
run in parallel with a short timeout and cached briefly, so a dropped feed
shows up within a minute without hammering anyone. Answers carry no secrets:
names, status, latency and a plain-language detail only.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, Response

router = APIRouter(prefix="/api/status", tags=["Status"])
CACHE_SECONDS = 45.0
SLOW_MS = 2500
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_lock = threading.Lock()


def _quote(symbol: str) -> Tuple[bool, str]:
    from loki.api.market_feed import quote_or_failure

    row = quote_or_failure(symbol, None, 30.0)
    if row.get("ok"):
        return True, f"{symbol} {row.get('price')}"
    return False, str(row.get("error") or "no quote")[:120]


def _gamma() -> Tuple[bool, str]:
    from loki.api import polymarket

    rows = polymarket._get("/events", {"active": "true", "closed": "false", "limit": 1})
    return bool(rows), "odds answering" if rows else "empty answer"


def _clob() -> Tuple[bool, str]:
    res = requests.get("https://clob.polymarket.com/time", timeout=(3, 5), allow_redirects=False)
    return res.status_code == 200, f"HTTP {res.status_code}"


def _ai() -> Tuple[bool, str]:
    from loki_v8.train import load_report

    uni = (load_report() or {}).get("universal") or {}
    ok = bool((uni.get("equity") or {}).get("verified") and (uni.get("portfolio") or {}).get("verified"))
    return ok, "verified profiles loaded" if ok else "no verified profile on this server"


def _accounts() -> Tuple[Optional[bool], str]:
    from loki.api import site_access

    if site_access.accounts_available():
        return True, "sign-in available"
    return None, "not set up on this server yet: add the Supabase settings to turn on sign-up and saved workspaces"


def _logins() -> Tuple[Optional[bool], str]:
    import os

    from loki.api import site_access

    public = bool(os.environ.get("VERCEL") or site_access.gate_enabled())
    if site_access.secret_is_configured() or not public:
        return True, "stable login secret"
    return None, "logins stay signed in (keyed to the access code); add LOKI_GATE_SECRET for independent signing"


Check = Tuple[str, str, Callable[[], Tuple[Optional[bool], str]]]
CHECKS: List[Check] = [
    ("stocks", "Stock & index prices (Yahoo)", lambda: _quote("SPY")),
    ("crypto", "Crypto prices (Binance)", lambda: _quote("BTC/USDT")),
    ("odds", "Polymarket odds", _gamma),
    ("books", "Polymarket order books", _clob),
    ("ai", "Trading AI profiles", _ai),
    ("accounts", "Accounts", _accounts),
    ("logins", "Login security", _logins),
]


def _run(check: Check) -> Dict[str, Any]:
    """ok, slow, setup (works once configured) or down. True/False/None from the check."""
    key, label, fn = check
    started = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a status light must never 500
        ok, detail = False, f"unreachable ({exc.__class__.__name__})"
    ms = int((time.perf_counter() - started) * 1000)
    if ok is None:
        state = "setup"
    elif not ok:
        state = "down"
    else:
        state = "slow" if ms > SLOW_MS and key in ("stocks", "crypto", "odds", "books") else "ok"
    return {"key": key, "label": label, "state": state, "latency_ms": ms, "detail": detail}


def snapshot(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _cache.get("snap")
        if hit and not force and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
        feeds = list(pool.map(_run, CHECKS))
    states = {f["state"] for f in feeds}
    worst = next((s for s in ("down", "slow", "setup") if s in states), "ok")
    snap = {"overall": worst, "checked_at": int(now), "feeds": feeds}
    with _lock:
        _cache["snap"] = (now, snap)
    return snap


@router.get("/feeds")
def feeds(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return snapshot()

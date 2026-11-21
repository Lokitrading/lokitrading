"""Live SOL/AUD and USDC/AUD: the terminal's money is Australian dollars.

* SOL/USD: Jupiter's price API (on-chain liquidity weighted), falling back to Coinbase SOL-USD.
* USDC/AUD: Coinbase's USDC-AUD market, trade by trade (a daily ECB rate is far too slow for
  scalping). USDC is the dollar leg on Solana, so SOL/AUD = SOL/USD x USDC/AUD.

Quotes are cached for a few seconds (``SOL_TTL_S`` / ``FX_TTL_S``); a source that fails falls back
to the next one, and a stale value is only ever served flagged ``live: False``.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
SOL_TTL_S = 3.0
FX_TTL_S = 10.0
STALE_AFTER_S = 120.0
_UA = {"User-Agent": "Loki-terminal", "Accept": "application/json"}
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, float, str]] = {}  # name -> (fetched_at, value, source)


def _get(url: str, timeout: float = 4.0) -> Any:
    r = requests.get(url, headers=_UA, timeout=timeout, allow_redirects=False)
    r.raise_for_status()
    return r.json()


def _sol_usd_jupiter() -> float:
    return float(_get(f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}")[SOL_MINT]["usdPrice"])


def _sol_usd_coinbase() -> float:
    return float(_get("https://api.exchange.coinbase.com/products/SOL-USD/ticker")["price"])


def _usdc_aud_coinbase() -> float:
    return float(_get("https://api.exchange.coinbase.com/products/USDC-AUD/ticker")["price"])


SOURCES: Dict[str, List[Tuple[str, Callable[[], float]]]] = {
    "sol_usd": [("jupiter", _sol_usd_jupiter), ("coinbase", _sol_usd_coinbase)],
    "usdc_aud": [("coinbase", _usdc_aud_coinbase)],
}
TTL = {"sol_usd": SOL_TTL_S, "usdc_aud": FX_TTL_S}


def _value(name: str, now: Optional[float] = None) -> Tuple[float, str, bool, float]:
    """(value, source, live, age_s) for one series, trying each source in order."""
    now = time.time() if now is None else now
    with _lock:
        hit = _cache.get(name)
    if hit and now - hit[0] < TTL[name]:
        return hit[1], hit[2], True, now - hit[0]
    for source, fetch in SOURCES[name]:
        try:
            value = fetch()
        except Exception:  # noqa: BLE001 - try the next source
            continue
        if value > 0:
            with _lock:
                _cache[name] = (now, value, source)
            return value, source, True, 0.0
    if hit and now - hit[0] < STALE_AFTER_S:
        return hit[1], hit[2], False, now - hit[0]
    raise RuntimeError(f"No live {name.replace('_', '/').upper()} price right now.")


def rates(now: Optional[float] = None) -> Dict[str, Any]:
    """Live SOL/USD, USDC/AUD and SOL/AUD, with their sources and freshness."""
    sol_usd, s_src, s_live, s_age = _value("sol_usd", now)
    usdc_aud, f_src, f_live, f_age = _value("usdc_aud", now)
    return {"sol_usd": round(sol_usd, 6), "usdc_aud": round(usdc_aud, 6), "sol_aud": round(sol_usd * usdc_aud, 6),
            "live": s_live and f_live, "sources": {"sol_usd": s_src, "usdc_aud": f_src},
            "age_s": {"sol_usd": round(s_age, 2), "usdc_aud": round(f_age, 2)}}


def lamports_to_aud(lamports: float, sol_aud: float) -> float:
    return lamports / LAMPORTS_PER_SOL * sol_aud


def sol_to_aud(sol: float, sol_aud: float) -> float:
    return sol * sol_aud


def aud_to_sol(aud: float, sol_aud: float) -> float:
    if sol_aud <= 0:
        raise ValueError("SOL/AUD must be positive")
    return aud / sol_aud


def aud_to_lamports(aud: float, sol_aud: float) -> int:
    return int(aud_to_sol(aud, sol_aud) * LAMPORTS_PER_SOL)

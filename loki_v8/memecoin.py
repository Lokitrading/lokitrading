"""Memecoin rules: plain, published trend rules on 24/7 crypto bars, long or cash only.

Three rule families, each with a few settings, on 4-hour or daily bars:

* breakout:  in the market after a close above the highest close of the last ``entry`` bars;
             out after a close below the lowest close of the last ``exit`` bars.
* trend:     in the market while the fast average is above the slow one and price is above the slow one.
* momentum:  in the market while the return over the last ``lookback`` bars is positive.

Each coin in the market gets an equal share of the book, scaled down so its recent volatility
does not exceed ``VOL_TARGET`` a year (a coin moving 150% a year gets about half its share).
The rest stays in cash. A signal is decided on a bar's close and earns from the next bar.
Every change of holding pays ``COST`` per side (Alpaca's 0.25% crypto fee plus spread).
Only long positions: Alpaca does not short crypto.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

COINS = ["DOGE", "SHIB", "PEPE", "BONK", "WIF", "TRUMP"]  # memecoins Alpaca can trade (paper or live)
RESEARCH_ONLY = ["FLOKI"]  # in the test, not tradable on Alpaca
COST = 0.0035  # per side, as a fraction of the amount traded
VOL_TARGET = 0.80  # a year
VOL_WINDOW_DAYS = 30
BARS_PER_YEAR = {"4h": 6 * 365, "1d": 365}

GRID: List[Dict[str, Any]] = (
    [{"family": "breakout", "bars": b, "entry": e, "exit": e // 2} for b, es in (("4h", (60, 120, 240)), ("1d", (20, 55))) for e in es]
    + [{"family": "trend", "bars": b, "fast": f, "slow": s} for b, fs in (("4h", ((30, 180), (60, 360))), ("1d", ((10, 50), (20, 100)))) for f, s in fs]
    + [{"family": "momentum", "bars": b, "lookback": n} for b, ns in (("4h", (42, 126, 252)), ("1d", (7, 21, 60))) for n in ns]
)


def label(rule: Dict[str, Any]) -> str:
    keys = [k for k in ("entry", "exit", "fast", "slow", "lookback") if k in rule]
    return f"{rule['family']} {rule['bars']} " + " ".join(f"{k} {rule[k]}" for k in keys)


def signal(close: pd.Series, rule: Dict[str, Any]) -> pd.Series:
    """1 while the rule holds the coin after this bar's close, 0 while in cash."""
    fam = rule["family"]
    if fam == "breakout":
        high = close.shift(1).rolling(rule["entry"]).max()
        low = close.shift(1).rolling(rule["exit"]).min()
        state, out = 0.0, np.zeros(len(close))
        c, h, lo = close.to_numpy(), high.to_numpy(), low.to_numpy()
        for i in range(len(c)):
            if state == 0 and not np.isnan(h[i]) and c[i] > h[i]:
                state = 1.0
            elif state == 1 and not np.isnan(lo[i]) and c[i] < lo[i]:
                state = 0.0
            out[i] = state
        return pd.Series(out, index=close.index)
    if fam == "trend":
        fast = close.ewm(span=rule["fast"], adjust=False).mean()
        slow = close.ewm(span=rule["slow"], adjust=False).mean()
        ready = np.arange(len(close)) >= rule["slow"]
        return ((fast > slow) & (close > slow) & ready).astype(float)
    if fam == "momentum":
        return (close / close.shift(rule["lookback"]) - 1 > 0).astype(float)
    raise ValueError(f"unknown rule family {fam}")


def weights(closes: Dict[str, pd.Series], rule: Dict[str, Any]) -> pd.DataFrame:
    """Holding per coin after each bar's close, as a fraction of the book (0 = cash)."""
    frame = pd.DataFrame(closes).sort_index()
    per_year = BARS_PER_YEAR[rule["bars"]]
    window = VOL_WINDOW_DAYS * per_year // 365
    rets = frame.pct_change(fill_method=None)
    vol = rets.rolling(window, min_periods=window // 2).std() * np.sqrt(per_year)
    sig = pd.DataFrame({c: signal(frame[c].dropna(), rule) for c in frame}).reindex(frame.index).fillna(0.0)
    listed = frame.notna() & vol.notna()  # a coin counts once it has a price and enough history for its volatility
    n = listed.sum(axis=1).replace(0, np.nan)
    scale = (VOL_TARGET / vol).clip(upper=1.0)
    w = (sig * scale).where(listed, 0.0).div(n, axis=0).fillna(0.0)
    return w


def book_returns(closes: Dict[str, pd.Series], rule: Dict[str, Any]) -> pd.Series:
    """The book's return per bar after costs: holdings decided on a close earn from the next bar."""
    frame = pd.DataFrame(closes).sort_index()
    w = weights(closes, rule)
    rets = frame.pct_change(fill_method=None).fillna(0.0)
    held = w.shift(1).fillna(0.0)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    return (held * rets).sum(axis=1) - COST * turnover.shift(1).fillna(0.0)


def hold_returns(closes: Dict[str, pd.Series]) -> pd.Series:
    """Equal money in every listed coin, rebalanced each bar, no costs: the plain buy-and-hold benchmark."""
    frame = pd.DataFrame(closes).sort_index()
    return frame.pct_change(fill_method=None).mean(axis=1).fillna(0.0)


def stats(returns: pd.Series, bars: str) -> Dict[str, Any]:
    r = returns.dropna()
    if len(r) < 2:
        return {}
    per_year = BARS_PER_YEAR[bars]
    equity = (1 + r).cumprod()
    years = len(r) / per_year
    cagr = equity.iloc[-1] ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1.0
    sd = r.std()
    return {"cagr_pct": round(float(cagr) * 100, 2), "sharpe": round(float(r.mean() / sd * np.sqrt(per_year)) if sd > 0 else 0.0, 3),
            "max_dd_pct": round(float((equity / equity.cummax() - 1).min() * 100), 2),
            "total_pct": round(float(equity.iloc[-1] - 1) * 100, 2), "from": str(r.index[0].date()), "to": str(r.index[-1].date())}


def live_state(closes: Dict[str, pd.Series], rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What the rule holds in each coin after the latest completed bar."""
    w = weights(closes, rule)
    last = w.iloc[-1] if len(w) else pd.Series(dtype=float)
    out = []
    for coin, series in closes.items():
        s = series.dropna()
        if s.empty:
            out.append({"coin": coin, "status": "no_data", "weight": 0.0})
            continue
        out.append({"coin": coin, "price": float(s.iloc[-1]), "as_of": str(s.index[-1]), "holding": bool(last.get(coin, 0) > 0),
                    "weight": round(float(last.get(coin, 0.0)), 4)})
    return out


def levels(close: pd.Series, rule: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """For a breakout rule: the close that would buy (above the entry high) and sell (below the exit low)."""
    if rule.get("family") != "breakout" or len(close) < rule["entry"]:
        return {"enter_above": None, "exit_below": None}
    return {"enter_above": float(close.iloc[-rule["entry"]:].max()), "exit_below": float(close.iloc[-rule["exit"]:].min())}


def read(spread_bps: Optional[float], depth_usd: Optional[float], imbalance: Optional[float], change_pct: Optional[float]) -> List[str]:
    """A plain checklist of what the order book and the last 24 hours show. Descriptions, not forecasts."""
    notes = []
    if depth_usd is not None:
        notes.append("deep book" if depth_usd >= 250_000 else "thin book: large orders move the price")
    if spread_bps is not None and spread_bps > 15:
        notes.append("wide spread")
    if imbalance is not None and abs(imbalance) >= 0.25:
        notes.append("more resting bids than asks" if imbalance > 0 else "more resting asks than bids")
    if change_pct is not None and abs(change_pct) >= 10:
        notes.append(f"big 24h move ({change_pct:+.1f}%)")
    return notes


def pick(scores: Dict[str, Optional[float]]) -> Optional[str]:
    """The rule with the best Sharpe on the choosing window (None when nothing scored)."""
    scored = {k: v for k, v in scores.items() if v is not None}
    return max(scored, key=lambda k: scored[k]) if scored else None

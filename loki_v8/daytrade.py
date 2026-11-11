"""LOKI v8 — Day Trader: intraday momentum with noise bands, flat every night.

The method is the published one (Zarattini, Aziz and Barbon, 2024, "Beat the
Market: An Effective Intraday Momentum Strategy for the S&P 500 ETF"), used
as written, with no tuning here:

* For each 5-minute slot of the session, measure how far price usually is
  from the day's open at that time of day: the average absolute move over the
  previous ``LOOKBACK`` sessions. That is the "noise" for the slot.
* The upper band is max(today's open, yesterday's close) * (1 + noise); the
  lower band is min(open, yesterday's close) * (1 - noise).
* Every 30 minutes: go long if price is above both the upper band and the
  session VWAP, short if below both the lower band and VWAP, otherwise flat.
* Orders fill at the next 5-minute bar's open. Everything is closed at the
  session's last bar: no position is ever held overnight.

One function, ``replay_day``, is used by the backtest and by the live plan,
so the website trades exactly what was tested. Costs are charged per side.

Honesty: tested on twelve liquid US stocks and funds over ten years, the
method made money on some and lost on others (see ``daytrade_record.json``).
It is weaker than the Portfolio AI and very sensitive to trading costs.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

LOOKBACK = 14  # sessions of history behind each slot's noise estimate
SLOTS = 78  # 5-minute bars from 09:30 to 16:00 New York time
DECIDE_EVERY = 6  # bars: decisions on the half hour
SESSION_TZ = "America/New_York"
ETF_COST = 1e-4  # per side: spread + slippage on the big index funds
STOCK_COST = 2e-4  # per side on large single stocks
ETFS = {"SPY", "QQQ", "IWM", "DIA"}


def cost_for(symbol: str) -> float:
    return ETF_COST if symbol.upper() in ETFS else STOCK_COST


def sessions(bars: pd.DataFrame, min_bars: int = 70) -> List[Tuple[pd.Timestamp, pd.DataFrame]]:
    """Split 5-minute bars into regular-hours sessions (New York time), oldest first."""
    if bars is None or bars.empty:
        return []
    df = bars.copy()
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.tz_convert(SESSION_TZ)
    df = df.between_time("09:30", "15:55")
    out = []
    for day, g in df.groupby(df.index.date):
        if len(g) >= min_bars:
            out.append((pd.Timestamp(day), g))
    return out


def slots_of(g: pd.DataFrame) -> np.ndarray:
    return np.asarray(((g.index.hour * 60 + g.index.minute) - 570) // 5, dtype=int)


def day_moves(g: pd.DataFrame) -> np.ndarray:
    """|close / session open - 1| in each 5-minute slot (NaN where a bar is missing)."""
    move = np.full(SLOTS, np.nan)
    close = g["close"].to_numpy(dtype=float)
    slot = slots_of(g)
    ok = (slot >= 0) & (slot < SLOTS)
    move[slot[ok]] = np.abs(close[ok] / float(g["open"].iloc[0]) - 1.0)
    return move


def noise_profile(history: Sequence[np.ndarray]) -> Optional[np.ndarray]:
    """Average move per slot over the last ``LOOKBACK`` sessions, or None without enough history."""
    if len(history) < LOOKBACK:
        return None
    with np.errstate(all="ignore"):
        prof = np.nanmean(np.array(history[-LOOKBACK:]), axis=0)
    return np.where(np.isfinite(prof), prof, 0.0)


def _vwap(g: pd.DataFrame) -> np.ndarray:
    volume = g["volume"].to_numpy(dtype=float)
    typical = g["vwap"].to_numpy(dtype=float) if "vwap" in g.columns else (
        (g["high"].to_numpy(dtype=float) + g["low"].to_numpy(dtype=float) + g["close"].to_numpy(dtype=float)) / 3.0)
    total = np.cumsum(volume)
    with np.errstate(all="ignore"):
        out = np.cumsum(typical * volume) / np.where(total > 0, total, np.nan)
    return np.where(np.isfinite(out), out, g["close"].to_numpy(dtype=float))


def replay_day(g: pd.DataFrame, noise: np.ndarray, prev_close: float, cost: float,
               sides: Tuple[int, ...] = (1, -1), session_complete: bool = True) -> Dict[str, Any]:
    """Run the rules over one session's bars (completed bars only).

    Returns the day's return after costs, the position now, the decisions made
    and the bands at the latest bar. With ``session_complete`` False (live, mid
    session) the open position is left open and marked to the last close.
    """
    slot = slots_of(g)
    o, c = g["open"].to_numpy(dtype=float), g["close"].to_numpy(dtype=float)
    day_open = float(o[0])
    safe = np.clip(slot, 0, SLOTS - 1)
    upper = max(day_open, prev_close) * (1.0 + noise[safe])
    lower = min(day_open, prev_close) * (1.0 - noise[safe])
    vwap = _vwap(g)
    pos, equity, turns = 0, 1.0, 0
    decisions: List[Dict[str, Any]] = []
    n = len(c)
    for k in range(n - 1):
        if slot[k] % DECIDE_EVERY == DECIDE_EVERY - 1:  # the bar that ends on :00 or :30
            want = (1 if (c[k] > max(upper[k], vwap[k]) and 1 in sides)
                    else -1 if (c[k] < min(lower[k], vwap[k]) and -1 in sides) else 0)
            if want != pos:
                equity *= 1.0 - cost * abs(want - pos)
                turns += abs(want - pos)
                decisions.append({"time": g.index[k + 1].isoformat(), "from": pos, "to": want,
                                  "price": round(float(o[k + 1]), 4)})
                pos = want
                equity *= 1.0 + pos * (c[k + 1] / o[k + 1] - 1.0)
                continue
        equity *= 1.0 + pos * (c[k + 1] / c[k] - 1.0)
    # A decision on the latest completed bar fills at the next bar's open, which has not printed yet.
    pending = None
    if not session_complete and n and slot[-1] % DECIDE_EVERY == DECIDE_EVERY - 1 and slot[-1] < SLOTS - 1:
        want = (1 if (c[-1] > max(upper[-1], vwap[-1]) and 1 in sides)
                else -1 if (c[-1] < min(lower[-1], vwap[-1]) and -1 in sides) else 0)
        if want != pos:
            pending = want
    if session_complete and pos:
        equity *= 1.0 - cost * abs(pos)
        turns += abs(pos)
        decisions.append({"time": g.index[-1].isoformat(), "from": pos, "to": 0, "price": round(float(c[-1]), 4),
                          "why": "session close"})
        pos = 0
    return {"return": equity - 1.0, "position": pos, "pending": pending, "turns": turns, "decisions": decisions,
            "upper": float(upper[-1]), "lower": float(lower[-1]), "vwap": float(vwap[-1]), "last": float(c[-1]),
            "open": day_open, "prev_close": float(prev_close)}


def backtest(bars: pd.DataFrame, cost: float, sides: Tuple[int, ...] = (1, -1)) -> pd.Series:
    """Daily returns after costs over every session with enough history behind it."""
    history: List[np.ndarray] = []
    out: Dict[pd.Timestamp, float] = {}
    prev_close: Optional[float] = None
    for day, g in sessions(bars):
        noise = noise_profile(history)
        if noise is not None and prev_close:
            out[day] = float(replay_day(g, noise, prev_close, cost, sides)["return"])
        history.append(day_moves(g))
        prev_close = float(g["close"].iloc[-1])
    return pd.Series(out, dtype=float)


def stats(returns: pd.Series) -> Dict[str, Any]:
    x = returns.dropna()
    if len(x) < 30 or float(x.std()) == 0.0:
        return {}
    eq = (1.0 + x).cumprod()
    active = x[x != 0]
    return {
        "cagr_pct": round(float((eq.iloc[-1] ** (252 / len(x)) - 1) * 100), 2),
        "sharpe": round(float(x.mean() / x.std() * math.sqrt(252)), 3),
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min() * 100), 2),
        "win_days_pct": round(float((active > 0).mean() * 100), 1) if len(active) else 0.0,
        "days_traded_pct": round(float(len(active) / len(x) * 100), 1),
        "days": int(len(x)),
    }


def live_state(bars: pd.DataFrame, symbol: str, now: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    """What the tested rules hold in ``symbol`` right now, from recent 5-minute bars."""
    now = (now or pd.Timestamp.now(tz="UTC")).tz_convert(SESSION_TZ)
    days = sessions(bars, min_bars=1)
    if len(days) < LOOKBACK + 1:
        return {"symbol": symbol, "status": "no_data", "position": 0}
    today = now.normalize().tz_localize(None)
    last_day, g = days[-1]
    history = [day_moves(d) for _, d in days[:-1] if len(d) >= 70]
    noise = noise_profile(history)
    prior = [d for _, d in days[:-1] if len(d) >= 70]
    if noise is None or not prior:
        return {"symbol": symbol, "status": "no_data", "position": 0}
    g = g[g.index + pd.Timedelta(minutes=5) <= now]  # completed bars only
    in_session = last_day == today and now.time() < pd.Timestamp("16:00").time() and len(g) > 0
    if not in_session:
        return {"symbol": symbol, "status": "closed", "position": 0, "as_of": str(days[-1][1].index[-1]),
                "last": round(float(days[-1][1]["close"].iloc[-1]), 4)}
    state = replay_day(g, noise, float(prior[-1]["close"].iloc[-1]), cost_for(symbol), session_complete=False)
    flat_time = now.time() >= pd.Timestamp("15:55").time()
    position = 0 if flat_time else int(state["position"])
    return {"symbol": symbol, "status": "open", "position": position, "pending": None if flat_time else state["pending"],
            "day_return_pct": round(state["return"] * 100, 3), "last": round(state["last"], 4),
            "upper": round(state["upper"], 4), "lower": round(state["lower"], 4), "vwap": round(state["vwap"], 4),
            "open": round(state["open"], 4), "prev_close": round(state["prev_close"], 4),
            "decisions": state["decisions"][-6:], "as_of": str(g.index[-1] + pd.Timedelta(minutes=5))}

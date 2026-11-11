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
FUTURES_COST = 0.5e-4  # per side on micro index futures: about one tick plus commission
ETFS = {"SPY", "QQQ", "IWM", "DIA"}
# Micro index futures and the fund whose regular-hours prices stand in for them in the test.
FUTURES: Dict[str, Dict[str, Any]] = {"MNQ": {"proxy": "QQQ", "name": "Micro E-mini Nasdaq-100", "multiplier": 2.0, "index": "NQ=F"},
           "MES": {"proxy": "SPY", "name": "Micro E-mini S&P 500", "multiplier": 5.0, "index": "ES=F"},
           "M2K": {"proxy": "IWM", "name": "Micro E-mini Russell 2000", "multiplier": 5.0, "index": "RTY=F"},
           "MYM": {"proxy": "DIA", "name": "Micro E-mini Dow", "multiplier": 0.5, "index": "YM=F"}}
# Learning from mistakes: what is known at a day's first signal, and whether such days made money.
FEATURES = ("gap", "absgap", "noise", "prev", "absprev", "side", "slot", "mon", "fri")
SKIP_BELOW = 0.40  # skip a day whose estimated chance of a winning day is under this


def cost_for(symbol: str) -> float:
    symbol = symbol.upper()
    return FUTURES_COST if symbol in FUTURES else ETF_COST if symbol in ETFS else STOCK_COST


def price_symbol(symbol: str) -> str:
    """The market whose 5-minute bars drive the rules for ``symbol`` (futures use their fund proxy)."""
    return str(FUTURES.get(symbol.upper(), {}).get("proxy", symbol.upper()))


def features(gap: float, noise_mean: float, prev_ret: float, weekday: int, side: int, slot: int) -> List[float]:
    return [gap * 100.0, abs(gap) * 100.0, noise_mean * 100.0, prev_ret * 100.0, abs(prev_ret) * 100.0,
            float(side), slot / float(SLOTS), 1.0 if weekday == 0 else 0.0, 1.0 if weekday == 4 else 0.0]


def fit_filter(rows: Sequence[Sequence[float]], wins: Sequence[float], l2: float = 1.0, steps: int = 300) -> Dict[str, Any]:
    """Small ridge-regularised logistic model: which first-signal conditions led to winning days."""
    x, y = np.asarray(rows, dtype=float), np.asarray(wins, dtype=float)
    mu, sd = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-6)  # a feature that never varies must not divide by zero
    z = np.c_[np.ones(len(x)), (x - mu) / sd]
    w = np.zeros(z.shape[1])
    for _ in range(steps):
        p = 1.0 / (1.0 + np.exp(-z @ w))
        w -= 0.5 * (z.T @ (p - y) / len(y) + l2 * np.r_[0.0, w[1:]] / len(y))
    return {"w": [round(float(v), 6) for v in w], "mu": [round(float(v), 6) for v in mu],
            "sd": [round(float(v), 6) for v in sd], "lessons": int(len(y))}


def rate(model: Dict[str, Any], row: Sequence[float]) -> float:
    """Estimated chance that a day starting like ``row`` ends as a winner."""
    z = (np.asarray(row, dtype=float) - np.asarray(model["mu"])) / np.asarray(model["sd"])
    return float(1.0 / (1.0 + math.exp(-(model["w"][0] + float(np.dot(model["w"][1:], z))))))


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
               sides: Tuple[int, ...] = (1, -1), session_complete: bool = True,
               gate: Optional[Dict[str, Any]] = None, prev_ret: float = 0.0) -> Dict[str, Any]:
    """Run the rules over one session's bars (completed bars only).

    Returns the day's return after costs, the position now, the decisions made
    and the bands at the latest bar. With ``session_complete`` False (live, mid
    session) the open position is left open and marked to the last close.

    ``gate`` ({"model", "threshold"}) is the learn-from-mistakes filter: at the
    day's first signal the setup is rated, and a poorly rated day is sat out.
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
    first: Optional[List[float]] = None
    rating: Optional[float] = None
    skipped = False
    weekday = int(g.index[0].dayofweek)
    noise_mean = float(np.mean(noise))
    n = len(c)
    for k in range(n - 1):
        if slot[k] % DECIDE_EVERY == DECIDE_EVERY - 1 and not skipped:  # the bar that ends on :00 or :30
            want = (1 if (c[k] > max(upper[k], vwap[k]) and 1 in sides)
                    else -1 if (c[k] < min(lower[k], vwap[k]) and -1 in sides) else 0)
            if want != pos and first is None:
                first = features(day_open / prev_close - 1.0, noise_mean, prev_ret, weekday, want, int(slot[k]) + 1)
                if gate:
                    rating = rate(gate["model"], first)
                    if rating < float(gate.get("threshold", SKIP_BELOW)):
                        skipped = True
                        decisions.append({"time": g.index[k + 1].isoformat(), "from": 0, "to": 0, "skipped": want,
                                          "why": "setup rated poorly from past mistakes", "rating": round(rating, 3)})
                        continue
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
    if not session_complete and not skipped and n and slot[-1] % DECIDE_EVERY == DECIDE_EVERY - 1 and slot[-1] < SLOTS - 1:
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
            "first": first, "rating": rating, "skipped": skipped,
            "upper": float(upper[-1]), "lower": float(lower[-1]), "vwap": float(vwap[-1]), "last": float(c[-1]),
            "open": day_open, "prev_close": float(prev_close)}


def backtest(bars: pd.DataFrame, cost: float, sides: Tuple[int, ...] = (1, -1)) -> pd.Series:
    """Daily returns after costs over every session with enough history behind it."""
    return backtest_frame(bars, cost, sides)["return"]


def backtest_frame(bars: pd.DataFrame, cost: float, sides: Tuple[int, ...] = (1, -1),
                   gate: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """Per day: return after costs, round trips and the first-signal features (for learning).

    With ``gate`` the fixed learn-from-mistakes model is applied to every day (used for the
    forward record, where the model was frozen before those days happened).
    """
    history: List[np.ndarray] = []
    rows: Dict[pd.Timestamp, Dict[str, Any]] = {}
    prev_close: Optional[float] = None
    prev_ret = 0.0
    for day, g in sessions(bars):
        noise = noise_profile(history)
        close = float(g["close"].iloc[-1])
        if noise is not None and prev_close:
            out = replay_day(g, noise, prev_close, cost, sides, prev_ret=prev_ret, gate=gate)
            rows[day] = {"return": float(out["return"]), "turns": int(out["turns"]), "first": out["first"],
                         "noise": float(np.mean(noise))}
        history.append(day_moves(g))
        prev_ret = close / prev_close - 1.0 if prev_close else 0.0
        prev_close = close
    return pd.DataFrame.from_dict(rows, orient="index", columns=["return", "turns", "first", "noise"])


def learn_from_mistakes(frame: pd.DataFrame, first_year: int = 2018, threshold: float = SKIP_BELOW,
                        min_lessons: int = 200) -> Tuple[pd.Series, Optional[Dict[str, Any]]]:
    """Walk-forward filter: each year is traded with a model fitted only on earlier years' days.

    Returns the filtered daily returns and the model fitted on every traded day (for live use).
    """
    traded = frame[frame["turns"] > 0]
    kept = frame["return"].copy()
    for year in range(first_year, int(frame.index[-1].year) + 1):
        past = traded[traded.index < pd.Timestamp(f"{year}-01-01")]
        now = traded[(traded.index >= pd.Timestamp(f"{year}-01-01")) & (traded.index < pd.Timestamp(f"{year + 1}-01-01"))]
        if len(past) < min_lessons or now.empty:
            continue
        model = fit_filter(list(past["first"]), (past["return"] > 0).astype(float).to_numpy())
        for day, row in now.iterrows():
            if rate(model, row["first"]) < threshold:
                kept.loc[day] = 0.0
    final = fit_filter(list(traded["first"]), (traded["return"] > 0).astype(float).to_numpy()) if len(traded) >= min_lessons else None
    return kept, final


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


def live_state(bars: pd.DataFrame, symbol: str, now: Optional[pd.Timestamp] = None,
               gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    prev_ret = (float(prior[-1]["close"].iloc[-1]) / float(prior[-2]["close"].iloc[-1]) - 1.0) if len(prior) > 1 else 0.0
    state = replay_day(g, noise, float(prior[-1]["close"].iloc[-1]), cost_for(symbol), session_complete=False,
                       gate=gate, prev_ret=prev_ret)
    flat_time = now.time() >= pd.Timestamp("15:55").time()
    position = 0 if flat_time else int(state["position"])
    return {"symbol": symbol, "status": "open", "position": position, "pending": None if flat_time else state["pending"],
            "day_return_pct": round(state["return"] * 100, 3), "last": round(state["last"], 4),
            "upper": round(state["upper"], 4), "lower": round(state["lower"], 4), "vwap": round(state["vwap"], 4),
            "open": round(state["open"], 4), "prev_close": round(state["prev_close"], 4),
            "decisions": state["decisions"][-6:], "as_of": str(g.index[-1] + pd.Timedelta(minutes=5)),
            "skipped": bool(state["skipped"]), "rating": None if state["rating"] is None else round(state["rating"], 3)}


def forward_record(bars: pd.DataFrame, symbol: str, since: str, gate: Optional[Dict[str, Any]] = None,
                   now: Optional[pd.Timestamp] = None) -> Dict[str, float]:
    """Results of completed sessions after ``since``: days the frozen rules had never seen."""
    now = (now or pd.Timestamp.now(tz="UTC")).tz_convert(SESSION_TZ)
    frame = backtest_frame(bars, cost_for(symbol), gate=gate)
    if frame.empty:
        return {}
    today = now.normalize().tz_localize(None)
    done = frame[(frame.index > pd.Timestamp(since)) & ((frame.index < today) | (now.time() >= pd.Timestamp("16:00").time()))]
    return {str(day.date()): float(value) for day, value in done["return"].items()}


def summarize_forward(per_market: Dict[str, Dict[str, float]], markets: int) -> Dict[str, Any]:
    """Equal-capital book over the forward days (a market without data that day counts as flat)."""
    days = sorted({d for series in per_market.values() for d in series})
    if not days or markets <= 0:
        return {"days": 0}
    daily = [sum(series.get(d, 0.0) for series in per_market.values()) / markets for d in days]
    equity, peak, worst = 1.0, 1.0, 0.0
    for r in daily:
        equity *= 1.0 + r
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    traded = [r for r in daily if r != 0.0]
    return {"days": len(days), "from": days[0], "to": days[-1], "return_pct": round((equity - 1.0) * 100, 2),
            "worst_drop_pct": round(worst * 100, 2), "winning_days": sum(1 for r in traded if r > 0),
            "losing_days": sum(1 for r in traded if r < 0),
            "daily": [{"day": d, "return_pct": round(r * 100, 3)} for d, r in zip(days, daily)][-30:]}

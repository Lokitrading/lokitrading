"""LOKI v8 — Portfolio AI: the verified strategy on every market at once.

One trend strategy on one symbol is bumpy. The same verified rules run on
many weakly-related markets (stocks, ETFs, gold, bonds, crypto) and then
combined smooth each other out. This module:

1. replays each market's universal profile with ``run_backtest`` (a
   "sleeve"), giving its daily return after costs and its daily exposure;
2. gives every active sleeve an equal share of capital, then scales the
   whole book toward a target annual volatility — capped every day so total
   holdings never exceed 100% of the account (no leverage, ever);
3. for live use, reports what that exact portfolio holds at the latest
   completed bar, so the paper autopilot holds what was tested.

Scale changes are charged ``COST_BPS`` per side on the traded notional.

Idle cash (2.58): whatever the book does not hold is parked in a 0-3 month
Treasury-bill fund (``CASH_ETF``). The replay credits yesterday's published
13-week bill yield, minus ``CASH_FEE_PCT`` a year and ``CASH_SWITCH_BPS`` on
every move in or out, so the record and the live plan hold the same thing.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .config import LokiConfig
from .data import is_crypto
from .strategy import EnsembleStrategy

VOL_WINDOW = 63  # trading days of portfolio returns for realised volatility
ANNUAL = 260  # union calendar mixes 252 equity and 365 crypto days
MAX_SCALE = 4.0  # sleeves at 1/N each; the 100% gross cap binds far earlier
COST_BPS = 15.0  # fees + slippage per side on scale-driven rebalancing
FEE_BPS = 10.0  # per side on sleeve fills, on top of slippage in fills
CASH_ETF = "SGOV"  # 0-3 month US Treasury bills; holds the cash the book is not using
CASH_YIELD_SYMBOL = "^IRX"  # 13-week T-bill yield, % a year
CASH_FEE_PCT = 0.14  # % a year: a conservative T-bill fund expense ratio
CASH_SWITCH_BPS = 2.0  # per side when cash moves in or out of the bill fund


def tbill_returns(index: pd.DatetimeIndex, yields: pd.Series) -> pd.Series:
    """Daily return of a T-bill fund on ``index`` from published yields (decided on yesterday's print)."""
    y = yields.astype(float).copy()
    norm = pd.DatetimeIndex(y.index)
    if norm.tz is not None:
        norm = norm.tz_localize(None)
    y.index = norm.normalize()
    y = y[~y.index.duplicated(keep="last")].sort_index()
    y = y.reindex(index, method="ffill").shift(1).fillna(0.0)
    days = pd.Series(index, index=index).diff().dt.days.fillna(1).clip(lower=1, upper=5)
    return ((y - CASH_FEE_PCT).clip(lower=0.0) / 100.0) * days / 365.0


def config_for(params: Dict[str, Any]) -> LokiConfig:
    base = {k: v for k, v in params.items() if k in LokiConfig.__dataclass_fields__ and k != "factor_weights"}
    return replace(LokiConfig(initial_cash=100_000.0), **base, factor_weights=dict(params.get("factor_weights") or {}))


def run_sleeve(df: pd.DataFrame, params: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """Replay one market: daily net returns, daily exposure, latest state."""
    cfg = config_for(params)
    bpy = 365 if is_crypto(symbol) else 252
    frame = EnsembleStrategy(cfg).score_frame(df)
    res = run_backtest(df, cfg, symbol=symbol, bars_per_year=bpy, frame=frame)
    equity = res.equity_curve
    if equity.empty:
        return {"symbol": symbol, "returns": pd.Series(dtype=float), "exposure": pd.Series(dtype=float), "state": None}
    idx = equity.index
    fees = pd.Series(0.0, index=idx)
    held = pd.Series(0.0, index=idx)
    open_trade = None
    for t in res.trades:
        for stamp, px in ((t.entry_time, t.entry_price), (t.exit_time, t.exit_price)):
            pos = idx.searchsorted(pd.Timestamp(stamp))
            if pos < len(fees):
                fees.iloc[pos] += px * t.qty * FEE_BPS / 10_000.0
        mask = (idx >= t.entry_time) & (idx < t.exit_time)
        held[mask] += t.qty
        if t.exit_reason == "end":
            open_trade = t
    net = equity - fees.cumsum()
    close = df["close"].reindex(idx).astype(float)
    exposure = (held * close / net).clip(lower=0.0).fillna(0.0)
    if open_trade is not None:
        # The replay force-closes at the end; for live use it is still open.
        exposure.iloc[-1] = float(open_trade.qty * close.iloc[-1] / net.iloc[-1]) if net.iloc[-1] > 0 else 0.0
    returns = net.pct_change().fillna(0.0)
    norm = pd.DatetimeIndex(idx)
    if norm.tz is not None:
        norm = norm.tz_localize(None)
    returns.index = exposure.index = norm.normalize()
    state = None
    if open_trade is not None:
        state = {
            "in_position": True,
            "entry_date": str(pd.Timestamp(open_trade.entry_time).date()),
            "entry_price": round(float(open_trade.entry_price), 6),
            "unrealized_pct": round((float(close.iloc[-1]) / float(open_trade.entry_price) - 1) * 100, 2),
        }
    else:
        state = {"in_position": False}
    state["last_close"] = round(float(close.iloc[-1]), 6)
    last_score = frame["score"].iloc[-1] if "score" in frame.columns else float("nan")
    state["score"] = round(float(last_score), 1) if np.isfinite(last_score) else None
    try:
        from .regime import regime_series

        state["regime"] = str(regime_series(frame).iloc[-1])
    except Exception:  # noqa: BLE001 - presentation extra only
        state["regime"] = None
    state["as_of"] = str(norm[-1].date())
    return {"symbol": symbol, "returns": returns, "exposure": exposure, "state": state}


def combine(sleeves: Dict[str, Dict[str, Any]], target_vol_pct: float, warmup: int = 300,
            vol_window: Optional[int] = None, cash_yield: Optional[pd.Series] = None) -> Dict[str, Any]:
    """Equal-capital sleeves, vol-targeted, gross exposure capped at 100%.

    With ``cash_yield`` (13-week bill yields, % a year) the unused cash earns
    T-bill interest; volatility targeting still sees only the markets.
    """
    R = pd.DataFrame({s: v["returns"] for s, v in sleeves.items()}).sort_index()
    E = pd.DataFrame({s: v["exposure"] for s, v in sleeves.items()}).reindex(R.index).ffill().fillna(0.0)
    active = pd.DataFrame(
        {s: R.index >= (v["returns"].index[min(warmup, len(v["returns"]) - 1)] if len(v["returns"]) else R.index[-1])
         for s, v in sleeves.items()},
        index=R.index,
    )
    R = R.fillna(0.0)
    n = active.sum(axis=1).clip(lower=1)
    ew = (R * active).sum(axis=1) / n
    gross_ew = (E * active).sum(axis=1) / n
    window = int(vol_window or VOL_WINDOW)
    vol = ew.rolling(window, min_periods=20).std().shift(1) * math.sqrt(ANNUAL)
    target = (target_vol_pct / 100.0 / vol).fillna(1.0)
    weekly = target.resample("W-FRI").last().reindex(ew.index, method="ffill").fillna(1.0)
    # Same-day cap: a sleeve's holdings for day t are decided at that day's
    # open from day t-1's signal, so the book can size around them exactly.
    cap = (1.0 / gross_ew.replace(0.0, np.nan)).fillna(MAX_SCALE)
    scale = np.minimum(weekly, cap).clip(lower=0.0, upper=MAX_SCALE)
    cost = scale.diff().abs().fillna(0.0) * gross_ew * COST_BPS / 10_000.0
    returns = ew * scale - cost
    gross = gross_ew * scale
    out = {"returns": returns, "scale": scale, "gross": gross, "active": active, "exposure": E,
           "n_active": n, "base_returns": ew}
    if cash_yield is not None and len(cash_yield):
        rf = tbill_returns(returns.index, cash_yield)
        cash = (1.0 - gross).clip(lower=0.0, upper=1.0)
        switch = cash.diff().abs().fillna(0.0) * CASH_SWITCH_BPS / 10_000.0
        out["returns"] = returns + cash.shift(1).fillna(0.0) * rf - switch
        last = cash_yield.dropna()
        out["cash_yield_pct"] = round(max(0.0, float(last.iloc[-1]) - CASH_FEE_PCT), 3) if len(last) else None
    return out


def window_stats(returns: pd.Series, start: str, end: str) -> Dict[str, Any]:
    x = returns[(returns.index >= start) & (returns.index <= end)]
    if len(x) < 30:
        return {}
    eq = (1 + x).cumprod()
    years = len(x) / ANNUAL
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    sd = x.std()
    return {
        "cagr_pct": round(float(cagr * 100), 2),
        "sharpe": round(float(x.mean() / sd * math.sqrt(ANNUAL)), 3) if sd > 0 else 0.0,
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min() * 100), 2),
        "profitable_months_pct": round(float((x.resample("ME").apply(lambda m: (1 + m).prod() - 1) > 0).mean() * 100), 1),
    }


def buy_hold_returns(frames: Dict[str, pd.DataFrame], index: pd.DatetimeIndex, active: pd.DataFrame) -> pd.Series:
    cols = {}
    for s, df in frames.items():
        c = df["close"].astype(float)
        idx = pd.DatetimeIndex(c.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        c.index = idx.normalize()
        cols[s] = c.pct_change()
    bh = pd.DataFrame(cols).reindex(index).fillna(0.0)
    n = active.sum(axis=1).clip(lower=1)
    return (bh * active).sum(axis=1) / n


def allocation(sleeves: Dict[str, Dict[str, Any]], book: Dict[str, Any], prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """What the tested portfolio holds now: per-market target weights (sum ≤ 1)."""
    scale_now = float(book["scale"].iloc[-1])
    active_now = book["active"].iloc[-1]
    n = max(1, int(active_now.sum()))
    rows: List[Dict[str, Any]] = []
    for s, v in sleeves.items():
        state = v.get("state") or {}
        expo = float(v["exposure"].iloc[-1]) if len(v["exposure"]) else 0.0
        weight = scale_now / n * expo if bool(active_now.get(s, False)) else 0.0
        rows.append({"symbol": s, "weight": weight, "sleeve_exposure": round(expo, 4), **state,
                     "price": (prices or {}).get(s) or state.get("last_close")})
    gross = sum(r["weight"] for r in rows)
    if gross > 1.0:  # exact daily cap for live use
        for r in rows:
            r["weight"] /= gross
        gross = 1.0
    for r in rows:
        r["weight"] = round(r["weight"], 5)
    rows.sort(key=lambda r: r["weight"], reverse=True)
    out = {"scale": round(scale_now, 4), "gross": round(gross, 4), "cash": round(1.0 - gross, 4),
           "active_markets": n, "positions": rows}
    if book.get("cash_yield_pct") is not None:
        # The unused share sits in Treasury bills, exactly as in the replay.
        out["cash_parking"] = {"symbol": CASH_ETF, "weight": round(max(0.0, 1.0 - gross), 5),
                               "yield_pct": book["cash_yield_pct"],
                               "what": "0-3 month US Treasury bills: the cash the AI is not using earns interest"}
    return out


def select_target_vol(sleeves: Dict[str, Dict[str, Any]], windows: Dict[str, Tuple[str, str]],
                      candidates: Tuple[float, ...] = (10.0, 15.0, 20.0, 25.0),
                      vol_windows: Tuple[int, ...] = (VOL_WINDOW,),
                      cash_yield: Optional[pd.Series] = None) -> Tuple[float, List[Dict[str, Any]]]:
    """Pick risk target (and vol look-back) by the WORSE of development/validation Sharpe."""
    trials = []
    for vw in vol_windows:
        for tv in candidates:
            book = combine(sleeves, tv, vol_window=vw, cash_yield=cash_yield)
            dev = window_stats(book["returns"], *windows["development"])
            val = window_stats(book["returns"], *windows["validation"])
            trials.append({"target_vol_pct": tv, "vol_window": vw, "development": dev, "validation": val,
                           "rank": round(min(dev.get("sharpe", -9), val.get("sharpe", -9)), 3)})
    best = max(trials, key=lambda t: (t["rank"], -t["target_vol_pct"]))
    return best["target_vol_pct"], [dict(t, chosen=t is best) for t in trials]


def calendar_years(returns: pd.Series, start: str = "2005-01-01") -> List[Dict[str, Any]]:
    """Return for every calendar year from ``start`` (the last one is year-to-date)."""
    x = returns[returns.index >= start]
    out = []
    for year, chunk in x.groupby(x.index.year):
        if len(chunk) < 20:
            continue
        out.append({"year": int(year), "return_pct": round(float(((1 + chunk).prod() - 1) * 100), 2),
                    "partial": bool(chunk.index[-1] < pd.Timestamp(f"{year}-12-24"))})
    return out


# Risk levels a user may pick. The protocol's own choice (best worst-case Sharpe on
# 2005-2022) is the default; the others trade deeper drops for more return and
# are published with their full record so the choice is informed, not hidden.
RISK_LEVELS = (("steady", "Steady", 10.0), ("balanced", "Balanced", 15.0), ("growth", "Growth", 20.0))


def risk_levels(sleeves: Dict[str, Dict[str, Any]], windows: Dict[str, Tuple[str, str]], vol_window: Optional[int],
                default_target: float, cash_yield: Optional[pd.Series] = None) -> List[Dict[str, Any]]:
    """The whole tested record at each selectable risk level (same rules, same costs)."""
    out = []
    first = min(a for a, _ in windows.values())
    for key, label, target in RISK_LEVELS:
        book = combine(sleeves, target, vol_window=vol_window, cash_yield=cash_yield)
        years = calendar_years(book["returns"], first)
        row: Dict[str, Any] = {"id": key, "label": label, "target_vol_pct": target,
                               "default": abs(target - float(default_target)) < 1e-9,
                               "average_invested": round(float(book["gross"].mean()), 3),
                               "whole_record": window_stats(book["returns"], first, "2100-01-01"),
                               "losing_years": [y for y in years if y["return_pct"] < 0 and not y["partial"]],
                               "calendar_years": years,
                               "expected_range": expected_range(book["returns"], windows["development"][0], windows["validation"][1])}
        for name, (a, b) in windows.items():
            row[name] = window_stats(book["returns"], a, b)
        out.append(row)
    return out


NO_HINDSIGHT = ("SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "BTC-USD", "ETH-USD")


def hindsight_check(sleeves: Dict[str, Dict[str, Any]], frames: Dict[str, pd.DataFrame],
                    windows: Dict[str, Tuple[str, str]], symbols: Tuple[str, ...] = NO_HINDSIGHT,
                    cash_yield: Optional[pd.Series] = None) -> Dict[str, Any]:
    """The same method on markets nobody had to pick with hindsight.

    The main universe includes individual stocks (Apple, Nvidia, ...) that are
    known winners today, which flatters any backtest that holds them. This
    re-runs the identical procedure (risk level chosen on development and
    validation only) on broad index funds, gold, bonds, Bitcoin and Ether, and
    reports it next to buying and holding the same markets.
    """
    picked = [s for s in symbols if s in sleeves and s in frames]
    if len(picked) < 4:
        return {}
    sub = {s: sleeves[s] for s in picked}
    target, trials = select_target_vol(sub, windows, vol_windows=(63, 126, 189, 252), cash_yield=cash_yield)
    vol_window = next(t["vol_window"] for t in trials if t.get("chosen"))
    book = combine(sub, target, vol_window=vol_window, cash_yield=cash_yield)
    bh = buy_hold_returns({s: frames[s] for s in picked}, book["returns"].index, book["active"])
    out: Dict[str, Any] = {"symbols": picked, "target_vol_pct": target, "vol_window": vol_window}
    for name, (a, b) in windows.items():
        out[name] = window_stats(book["returns"], a, b)
        out[name + "_buy_hold"] = window_stats(bh, a, b)
    h, hb = out.get("holdout") or {}, out.get("holdout_buy_hold") or {}
    if h and hb:
        gap = h["sharpe"] - hb["sharpe"]
        verdict = ("beats" if gap > 0.1 else "matches" if gap >= -0.1 else "trails") + " buying and holding on risk-adjusted return"
        if h["max_dd_pct"] > hb["max_dd_pct"] * 0.6:  # drawdowns are negative: a smaller worst drop
            verdict += ", with much smaller drops"
        out["verdict"] = verdict
    return out


# --- Self-knowledge: uncertainty and health ------------------------------------
def bootstrap_ci(returns: pd.Series, n: int = 2000, block: int = 21, seed: int = 7,
                 level: float = 0.90) -> Dict[str, Any]:
    """Block-bootstrap interval for yearly return and Sharpe of a return series.

    Resamples 21-day blocks (keeps volatility clustering) to show how much the
    headline number could have differed by luck alone.
    """
    x = returns.dropna().to_numpy()
    if len(x) < block * 4:
        return {}
    rng = np.random.default_rng(seed)
    starts_max = len(x) - block
    k = int(np.ceil(len(x) / block))
    cagrs, sharpes = [], []
    for _ in range(n):
        idx = (rng.integers(0, starts_max + 1, size=k)[:, None] + np.arange(block)).ravel()[: len(x)]
        sample = x[idx]
        growth = float(np.prod(1 + sample))
        years = len(sample) / ANNUAL
        cagrs.append((growth ** (1 / years) - 1) * 100 if growth > 0 else -100.0)
        sd = sample.std()
        sharpes.append(sample.mean() / sd * math.sqrt(ANNUAL) if sd > 0 else 0.0)
    lo, hi = (1 - level) / 2 * 100, (1 + level) / 2 * 100
    return {
        "level": level,
        "cagr_pct": [round(float(np.percentile(cagrs, lo)), 2), round(float(np.percentile(cagrs, hi)), 2)],
        "sharpe": [round(float(np.percentile(sharpes, lo)), 3), round(float(np.percentile(sharpes, hi)), 3)],
        "probability_profitable": round(float(np.mean(np.array(cagrs) > 0)), 3),
    }


def _window_metrics(r: pd.Series) -> Tuple[float, float]:
    sd = r.std()
    sharpe = float(r.mean() / sd * math.sqrt(ANNUAL)) if sd > 0 else 0.0
    eq = (1 + r).cumprod()
    return sharpe, float((eq / eq.cummax() - 1).min() * 100)


def expected_range(returns: pd.Series, start: str, end: str, window: int = 252, step: int = 5) -> Dict[str, Any]:
    """Distribution of rolling 1-year Sharpe and worst drop inside a reference period."""
    x = returns[(returns.index >= start) & (returns.index <= end)]
    sharpes, drops = [], []
    for i in range(window, len(x) + 1, step):
        s, d = _window_metrics(x.iloc[i - window:i])
        sharpes.append(s); drops.append(d)
    if len(sharpes) < 20:
        return {}
    pct = lambda arr, q: round(float(np.percentile(arr, q)), 3)  # noqa: E731
    return {"window_days": window, "reference": [start, end], "samples": len(sharpes),
            "sharpe_p10": pct(sharpes, 10), "sharpe_p50": pct(sharpes, 50), "sharpe_p90": pct(sharpes, 90),
            "drop_p10": pct(drops, 10), "drop_p50": pct(drops, 50),
            "sharpe_sorted": [round(v, 3) for v in sorted(sharpes)]}


def health(returns: pd.Series, reference: Dict[str, Any], window: int = 252) -> Dict[str, Any]:
    """Is the last year normal for this strategy, judged against its own history?"""
    if not reference or len(returns) < window:
        return {"status": "unknown", "detail": "not enough history to judge"}
    sharpe, drop = _window_metrics(returns.iloc[-window:])
    ranks = reference.get("sharpe_sorted") or []
    percentile = float(np.searchsorted(ranks, sharpe) / len(ranks) * 100) if ranks else None
    if percentile is None:
        status = "unknown"
    elif percentile < 10 or drop < reference["drop_p10"]:
        status = "below_range"
    elif percentile > 90:
        status = "above_range"
    else:
        status = "normal"
    words = {
        "normal": "behaving within its normal historical range",
        "below_range": "weaker than 90% of its historical years — treat with extra caution",
        "above_range": "stronger than 90% of its historical years — unusually good, unlikely to last",
        "unknown": "not enough history to judge",
    }
    return {"status": status, "last_year_sharpe": round(sharpe, 3), "last_year_worst_drop_pct": round(drop, 2),
            "percentile_vs_history": None if percentile is None else round(percentile, 1),
            "expected_sharpe_range": [reference.get("sharpe_p10"), reference.get("sharpe_p90")],
            "detail": words[status]}


def recent_changes(sleeves: Dict[str, Dict[str, Any]], bars: int = 6) -> List[Dict[str, Any]]:
    """Entries and exits each market's verified rules made over its last few completed bars."""
    out: List[Dict[str, Any]] = []
    for symbol, sleeve in sleeves.items():
        e = sleeve["exposure"]
        if len(e) < 2:
            continue
        tail = e.iloc[-(bars + 1):]
        for (d0, v0), (d1, v1) in zip(tail.items(), list(tail.items())[1:]):
            if v0 <= 1e-9 < v1:
                out.append({"symbol": symbol, "action": "entered", "date": str(d1.date())})
            elif v1 <= 1e-9 < v0:
                out.append({"symbol": symbol, "action": "exited", "date": str(d1.date())})
    out.sort(key=lambda c: c["date"], reverse=True)
    return out

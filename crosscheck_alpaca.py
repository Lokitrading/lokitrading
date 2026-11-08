"""Independent-data check: does the AI's edge survive a different data provider?

The universal profiles were trained on Yahoo Finance history. This script
downloads Alpaca's own daily bars (split/dividend adjusted SIP data for stocks
from 2016, Alpaca crypto from 2021), runs the CURRENT live profiles on them with
the same fees and slippage, and compares the result with Yahoo over exactly the
same dates. Nothing is tuned here: it is a read-only robustness test.

    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python crosscheck_alpaca.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import requests

import train_long_history as tlh
from loki.api.alpaca_ai import CRYPTO
from loki_v8.backtest import run_backtest
from loki_v8.strategy import EnsembleStrategy

DATA = "https://data.alpaca.markets"
SNAPSHOT = Path(__file__).parent / "loki_v8" / "training_report_snapshot.json"
WINDOWS = {"equity": ("2017-01-01", "2100-01-01"), "crypto": ("2022-01-01", "2100-01-01")}  # after a year of warm-up


def _get(session: requests.Session, url: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    token = None
    while True:
        query = dict(params, **({"page_token": token} if token else {}))
        payload = session.get(url, params=query, timeout=60).json()
        chunk = payload.get("bars") or []
        if isinstance(chunk, dict):  # crypto endpoint keys bars by symbol
            chunk = next(iter(chunk.values()), []) if chunk else []
        bars.extend(chunk)
        token = payload.get("next_page_token")
        if not token:
            return bars


def alpaca_history(session: requests.Session, symbol: str) -> pd.DataFrame:
    if symbol in CRYPTO:
        bars = _get(session, f"{DATA}/v1beta3/crypto/us/bars",
                    {"symbols": CRYPTO[symbol], "timeframe": "1Day", "start": "2021-01-01", "limit": 10000})
    else:
        bars = _get(session, f"{DATA}/v2/stocks/{symbol}/bars",
                    {"timeframe": "1Day", "start": "2016-01-01", "adjustment": "all", "feed": "sip", "limit": 10000})
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True).dt.tz_localize(None).dt.normalize())
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def run(df: pd.DataFrame, params: Dict[str, Any], kind: str, symbol: str) -> Dict[str, Any]:
    bpy = 365 if kind == "crypto" else 252
    cfg = tlh.config_for(params)
    res = run_backtest(df, cfg, symbol=symbol, bars_per_year=bpy, frame=EnsembleStrategy(cfg).score_frame(df))
    equity = tlh._fee_adjusted_equity(res.equity_curve, res.trades)
    tlh._apply_fees(res.trades)
    start, end = WINDOWS[kind]
    return {"strategy": tlh._period_stats(equity, res.trades, start, end, bpy),
            "buy_hold": tlh._buy_hold(df, start, end, bpy)}


def main() -> int:
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        print("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY.", file=sys.stderr)
        return 2
    session = requests.Session()
    session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    universal = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["universal"]
    from loki_v8 import evolution

    rows = []
    for kind, universe in (("equity", tlh.EQUITY_UNIVERSE), ("crypto", tlh.CRYPTO_UNIVERSE)):
        params = evolution.grid_params(universal[kind]["params"])
        for symbol in universe:
            if symbol not in CRYPTO and kind == "crypto":
                continue  # BNB: not on Alpaca
            alp = alpaca_history(session, symbol)
            yah = tlh.load_history(symbol, Path("state/history"))
            first = max(alp.index[0], yah.index[0]) if len(alp) else None
            if first is None or len(alp) < 300:
                continue
            yah = yah[yah.index >= first]
            alp = alp[alp.index >= first]
            a, y = run(alp, params, kind, symbol), run(yah, params, kind, symbol)
            rows.append({"kind": kind, "symbol": symbol, "days": len(alp),
                         "alpaca": a["strategy"], "yahoo": y["strategy"], "buy_hold_alpaca": a["buy_hold"]})
    print(f"{'symbol':9} {'Alpaca CAGR':>11} {'Yahoo CAGR':>10} {'Alp Sharpe':>10} {'Yah Sharpe':>10} {'Alp maxDD':>9} {'trades A/Y':>10}")
    for r in rows:
        a, y = r["alpaca"], r["yahoo"]
        print(f"{r['symbol']:9} {a['cagr_pct']:>11} {y['cagr_pct']:>10} {a['sharpe']:>10} {y['sharpe']:>10} {a['max_dd_pct']:>9} {a['trades']:>4}/{y['trades']:<4}")
    summary = {}
    for kind in ("equity", "crypto"):
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        a_cagr = [r["alpaca"]["cagr_pct"] for r in sub]
        y_cagr = [r["yahoo"]["cagr_pct"] for r in sub]
        summary[kind] = {
            "symbols": len(sub), "window_start": WINDOWS[kind][0],
            "alpaca_mean_cagr_pct": round(float(np.mean(a_cagr)), 2), "yahoo_mean_cagr_pct": round(float(np.mean(y_cagr)), 2),
            "alpaca_mean_sharpe": round(float(np.mean([r["alpaca"]["sharpe"] for r in sub])), 3),
            "yahoo_mean_sharpe": round(float(np.mean([r["yahoo"]["sharpe"] for r in sub])), 3),
            "alpaca_profitable": sum(1 for c in a_cagr if c > 0),
            "cagr_correlation": round(float(np.corrcoef(a_cagr, y_cagr)[0, 1]), 3) if len(sub) > 2 else None,
            "mean_abs_cagr_gap_pct": round(float(np.mean(np.abs(np.array(a_cagr) - np.array(y_cagr)))), 2),
        }
    print("\n" + json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

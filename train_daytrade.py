"""Build the Day Trader's tested record (loki_v8/daytrade_record.json).

Protocol
--------
* Data: 5-minute bars from Alpaca's market-data API, 2016 onward (keys from
  APCA_API_KEY_ID / APCA_API_SECRET_KEY; market data only, no orders).
* The rules are the published noise-band method, used as written (no tuning).
* Symbols are chosen on 2016-2021 only (Sharpe after costs above 0.3) and
  judged on 2022 onward, which the choice never saw.

Usage:
    python train_daytrade.py                       # download, then write the record
    python train_daytrade.py --cache DIR           # reuse/keep downloaded bars in DIR
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from loki_v8 import daytrade

CANDIDATES = ["SPY", "QQQ", "IWM", "DIA", "NVDA", "AAPL", "TSLA", "MSFT", "AMZN", "META", "GOOGL", "AMD"]
SELECT_END, TEST_START = "2021-12-31", "2022-01-01"
MIN_SELECT_SHARPE = 0.3
# Simpler day-trading rules tested first on hourly bars (16 stocks/funds, 8 coins); all lost after costs.
REJECTED = [
    "opening-range breakout (first-hour high/low, stop at the other side): -5.8%/yr on unseen data",
    "first-hour momentum held to the close: -9.3%/yr",
    "last-half-hour momentum: -11.8%/yr (costs alone exceed the edge)",
    "fading the opening gap: -6.7%/yr; following the gap: -4.1%/yr",
    "crypto 24-hour breakout: -16.7%/yr; crypto dip-buying (RSI): -9.2%/yr; crypto hourly trend: -51.5%/yr",
]


def fetch(symbol: str, start: str = "2016-01-01T00:00:00Z") -> Optional[pd.DataFrame]:
    import requests

    headers = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}
    rows, token = [], None
    while True:
        params = {"timeframe": "5Min", "start": start, "limit": 10000, "adjustment": "all", "feed": "sip"}
        if token:
            params["page_token"] = token
        for attempt in range(6):
            try:
                res = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars", headers=headers, params=params, timeout=60)
                break
            except requests.RequestException:
                time.sleep(2 + 2 * attempt)
        else:
            return None
        if res.status_code != 200:
            print(symbol, "HTTP", res.status_code)
            return None
        body = res.json()
        rows += body.get("bars") or []
        token = body.get("next_page_token")
        if not token:
            break
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"])
    return df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})


def load(symbol: str, cache: Optional[Path]) -> Optional[pd.DataFrame]:
    path = cache / f"{symbol}.pkl" if cache else None
    if path and path.exists():
        return pickle.loads(path.read_bytes())
    df = fetch(symbol)
    if df is not None and path:
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(df))
    return df


def build(cache: Optional[Path]) -> Dict[str, Any]:
    daily: Dict[str, pd.Series] = {}
    rows = []
    for symbol in CANDIDATES:
        bars = load(symbol, cache)
        if bars is None or bars.empty:
            print(symbol, "skipped: no data")
            continue
        cost = daytrade.cost_for(symbol)
        net = daytrade.backtest(bars, cost)
        gross = daytrade.backtest(bars, 0.0)
        daily[symbol] = net
        select = daytrade.stats(net[net.index <= SELECT_END])
        rows.append({"symbol": symbol, "cost_bps_per_side": round(cost * 1e4, 1), "select": select,
                     "test": daytrade.stats(net[net.index >= TEST_START]), "all": daytrade.stats(net),
                     "before_costs": daytrade.stats(gross),
                     "chosen": bool(select and select["sharpe"] > MIN_SELECT_SHARPE)})
        print(symbol, rows[-1]["select"], rows[-1]["test"], flush=True)
    chosen = [r["symbol"] for r in rows if r["chosen"]]
    frame = pd.DataFrame(daily).fillna(0.0)
    book = frame[chosen].mean(axis=1)
    everything = frame.mean(axis=1)
    years = [{"year": int(y), "return_pct": round(float(((1 + x).prod() - 1) * 100), 2),
              "unseen": bool(y >= int(TEST_START[:4])), "partial": bool(x.index[-1] < pd.Timestamp(f"{y}-12-24"))}
             for y, x in book.groupby(book.index.year)]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "Intraday momentum with noise bands (Zarattini, Aziz, Barbon 2024), as published: 14-session noise, "
                  "decisions every 30 minutes, VWAP filter, long and short, flat at the close.",
        "data": "5-minute bars, regular US hours, Alpaca market data",
        "data_through": str(frame.index[-1].date()),
        "windows": {"select": ["2016-01-01", SELECT_END], "test": [TEST_START, "2100-01-01"]},
        "selection_rule": f"Sharpe after costs above {MIN_SELECT_SHARPE} on 2016-2021",
        "symbols": chosen,
        "candidates": rows,
        "book": {"select": daytrade.stats(book[book.index <= SELECT_END]), "test": daytrade.stats(book[book.index >= TEST_START]),
                 "all": daytrade.stats(book)},
        "all_candidates_book": {"test": daytrade.stats(everything[everything.index >= TEST_START]), "all": daytrade.stats(everything)},
        "calendar_years": years,
        "rejected_rules": REJECTED,
        "verified": bool(daytrade.stats(book[book.index >= TEST_START]).get("cagr_pct", -1) > 0),
        "caveats": [
            "Simulated on historical data with assumed costs; not real trading.",
            "Very sensitive to costs: doubling the assumed cost removes most of the edge.",
            "It lost money on 7 of the 12 symbols tested; the book holds only those that worked on 2016-2021.",
            "No leverage. Short positions need a margin account at a real broker.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=None, help="directory of downloaded bars to reuse/keep")
    args = parser.parse_args()
    record = build(args.cache)
    path = Path(__file__).parent / "loki_v8" / "daytrade_record.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print("record written:", path, "| book test", record["book"]["test"])


if __name__ == "__main__":
    main()

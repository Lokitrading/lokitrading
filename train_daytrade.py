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


LEARN_START = "2018-01-01"  # the filter needs two years of its own trades before it acts


def _sharpe(x: pd.Series) -> float:
    return float(daytrade.stats(x).get("sharpe", -9.0))


def _years(book: pd.Series) -> list:
    return [{"year": int(y), "return_pct": round(float(((1 + x).prod() - 1) * 100), 2),
             "unseen": bool(y >= int(TEST_START[:4])), "partial": bool(x.index[-1] < pd.Timestamp(f"{y}-12-24"))}
            for y, x in book.groupby(book.index.year)]


def _windows(x: pd.Series) -> Dict[str, Any]:
    return {"select": daytrade.stats(x[x.index <= SELECT_END]), "test": daytrade.stats(x[x.index >= TEST_START]),
            "all": daytrade.stats(x)}


def _shuffle_control(frames: Dict[str, pd.DataFrame], symbols: list, real: pd.Series, runs: int = 30) -> Dict[str, Any]:
    """Would a filter trained on scrambled outcomes have done as well? If so, the 'learning' is luck."""
    import numpy as np

    def window(x: pd.Series, a: str, b: str) -> float:
        return _sharpe(x[(x.index >= a) & (x.index <= b)])

    beat = {"select": 0, "test": 0}
    for seed in range(runs):
        cols = {}
        for symbol in symbols:
            frame = frames[symbol].copy()
            traded = frame["turns"] > 0
            rng = np.random.default_rng(seed)
            # Scramble which days count as wins, year by year, keeping every year's win rate.
            shuffled = frame["return"].copy()
            for _, idx in frame[traded].groupby(frame[traded].index.year).groups.items():
                shuffled.loc[idx] = rng.permutation(frame.loc[idx, "return"].to_numpy())
            fake = frame.assign(**{"return": shuffled})
            kept_fake, _ = daytrade.learn_from_mistakes(fake)
            cols[symbol] = frame["return"].where(kept_fake != 0.0, 0.0).where(traded, frame["return"])
        book = pd.DataFrame(cols).fillna(0.0).mean(axis=1)
        beat["select"] += window(real, LEARN_START, SELECT_END) > window(book, LEARN_START, SELECT_END)
        beat["test"] += window(real, TEST_START, "2100-01-01") > window(book, TEST_START, "2100-01-01")
    return {"runs": runs, "real_beat_shuffled_select": int(beat["select"]), "real_beat_shuffled_test": int(beat["test"])}


def _book(frames: Dict[str, pd.DataFrame], symbols: list, label: str) -> Dict[str, Any]:
    """Equal-capital book over ``symbols``; the mistake filter is kept only if it helped before 2022."""
    plain = pd.DataFrame({s: frames[s]["return"] for s in symbols}).fillna(0.0).mean(axis=1)
    learned, models = {}, {}
    for symbol in symbols:
        learned[symbol], models[symbol] = daytrade.learn_from_mistakes(frames[symbol])
    filtered = pd.DataFrame(learned).fillna(0.0).mean(axis=1)

    def learn_window(x: pd.Series) -> float:
        return _sharpe(x[(x.index >= LEARN_START) & (x.index <= SELECT_END)])

    kept = bool(all(models.values()) and learn_window(filtered) > learn_window(plain) + 0.05)
    control = _shuffle_control(frames, symbols, filtered) if kept else None
    if control and min(control["real_beat_shuffled_select"], control["real_beat_shuffled_test"]) < 0.9 * control["runs"]:
        kept = False  # not clearly better than a filter trained on noise
    final = filtered if kept else plain
    print(f"== {label}: filter {'KEPT' if kept else 'off'} | plain test {daytrade.stats(plain[plain.index >= TEST_START])} "
          f"| final test {daytrade.stats(final[final.index >= TEST_START])}", flush=True)
    return {
        "symbols": symbols, **_windows(final), "plain": _windows(plain), "calendar_years": _years(final),
        "learning": {
            "kept": kept, "threshold": daytrade.SKIP_BELOW, "features": list(daytrade.FEATURES),
            "how": "Each year is traded with a model fitted only on earlier years' days; it skips a day whose first "
                   "signal looks like past losing days. Kept only if it beat the plain rules on 2018-2021 and "
                   "beat filters trained on scrambled outcomes.",
            "select_window_sharpe": {"plain": round(learn_window(plain), 3), "filtered": round(learn_window(filtered), 3)},
            "shuffle_control": control,
            "models": {s: m for s, m in models.items()} if kept else {},
            "lessons": {s: (m or {}).get("lessons", 0) for s, m in models.items()},
        },
    }


def build(cache: Optional[Path]) -> Dict[str, Any]:
    frames: Dict[str, pd.DataFrame] = {}
    futures_frames: Dict[str, pd.DataFrame] = {}
    rows, futures_rows = [], []
    proxies = {v["proxy"]: k for k, v in daytrade.FUTURES.items()}
    for symbol in CANDIDATES:
        bars = load(symbol, cache)
        if bars is None or bars.empty:
            print(symbol, "skipped: no data")
            continue
        cost = daytrade.cost_for(symbol)
        frame = daytrade.backtest_frame(bars, cost)
        gross = daytrade.backtest(bars, 0.0)
        frames[symbol] = frame
        net = frame["return"]
        select = daytrade.stats(net[net.index <= SELECT_END])
        rows.append({"symbol": symbol, "cost_bps_per_side": round(cost * 1e4, 1), "select": select,
                     "test": daytrade.stats(net[net.index >= TEST_START]), "all": daytrade.stats(net),
                     "before_costs": daytrade.stats(gross),
                     "chosen": bool(select and select["sharpe"] > MIN_SELECT_SHARPE)})
        print(symbol, rows[-1]["select"], rows[-1]["test"], flush=True)
        if symbol in proxies:  # the same prices at futures costs stand in for the micro contract
            contract = proxies[symbol]
            fframe = daytrade.backtest_frame(bars, daytrade.FUTURES_COST)
            futures_frames[contract] = fframe
            fnet = fframe["return"]
            fselect = daytrade.stats(fnet[fnet.index <= SELECT_END])
            futures_rows.append({"symbol": contract, "name": daytrade.FUTURES[contract]["name"], "proxy": symbol,
                                 "cost_bps_per_side": round(daytrade.FUTURES_COST * 1e4, 2), "select": fselect,
                                 "test": daytrade.stats(fnet[fnet.index >= TEST_START]), "all": daytrade.stats(fnet),
                                 "chosen": bool(fselect and fselect["sharpe"] > MIN_SELECT_SHARPE)})
            print(contract, futures_rows[-1]["select"], futures_rows[-1]["test"], flush=True)
    chosen = [r["symbol"] for r in rows if r["chosen"]]
    stock_book = _book(frames, chosen, "stocks")
    futures_chosen = [r["symbol"] for r in futures_rows if r["chosen"]]
    futures_book = _book(futures_frames, futures_chosen, "futures") if futures_chosen else {}
    everything = pd.DataFrame({s: f["return"] for s, f in frames.items()}).fillna(0.0).mean(axis=1)
    data_through = str(max(f.index[-1] for f in frames.values()).date())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "Intraday momentum with noise bands (Zarattini, Aziz, Barbon 2024), as published: 14-session noise, "
                  "decisions every 30 minutes, VWAP filter, long and short, flat at the close.",
        "data": "5-minute bars, regular US hours, Alpaca market data",
        "data_through": data_through,
        "windows": {"select": ["2016-01-01", SELECT_END], "test": [TEST_START, "2100-01-01"]},
        "selection_rule": f"Sharpe after costs above {MIN_SELECT_SHARPE} on 2016-2021",
        "symbols": chosen,
        "candidates": rows,
        "book": {k: stock_book[k] for k in ("select", "test", "all")},
        "book_plain": stock_book["plain"],
        "learning": stock_book["learning"],
        "all_candidates_book": {"test": daytrade.stats(everything[everything.index >= TEST_START]), "all": daytrade.stats(everything)},
        "calendar_years": stock_book["calendar_years"],
        "futures": {
            "symbols": futures_chosen, "candidates": futures_rows,
            "book": {k: futures_book[k] for k in ("select", "test", "all")} if futures_book else {},
            "book_plain": futures_book.get("plain", {}), "learning": futures_book.get("learning", {}),
            "calendar_years": futures_book.get("calendar_years", []),
            "contracts": {k: daytrade.FUTURES[k] for k in futures_chosen},
            "note": "Tested on each contract's index fund during regular US hours (09:30-16:00 New York) at futures "
                    "costs. Futures also trade overnight; this method does not. A fund is a close stand-in, not the "
                    "contract itself.",
        },
        "rejected_rules": REJECTED + [
            "adaptive market switch (trade a market only while its own trailing 3-12 month result is positive): "
            "+2.5% to +4.2%/yr on unseen data vs +5.8% for the fixed list",
            "volatility-scaled position size: Sharpe 0.61 vs 0.55 on unseen data, too small to matter",
        ],
        "verified": bool(stock_book["test"].get("cagr_pct", -1) > 0),
        "caveats": [
            "Simulated on historical data with assumed costs; not real trading.",
            "Very sensitive to costs: doubling the assumed cost removes most of the edge.",
            "It lost money on 7 of the 12 symbols tested; the book holds only those that worked on 2016-2021.",
            "No leverage in the test. Short positions need a margin account; futures need a futures account.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=None, help="directory of downloaded bars to reuse/keep")
    args = parser.parse_args()
    record = build(args.cache)
    path = Path(__file__).parent / "loki_v8" / "daytrade_record.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print("record written:", path, "| book test", record["book"]["test"], "| futures test", record["futures"].get("book", {}).get("test"))


if __name__ == "__main__":
    main()

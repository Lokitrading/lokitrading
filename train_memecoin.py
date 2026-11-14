"""Test the memecoin rules and write loki_v8/memecoin_record.json.

Protocol (fixed before any test result was seen):

* Data: Binance's public 4-hour and daily bars for DOGE, SHIB, PEPE, BONK, WIF, TRUMP (the
  memecoins Alpaca trades) and FLOKI (research only), from each coin's listing to today.
* Rules: the grid in ``loki_v8/memecoin.py`` (breakout, trend and momentum, a few settings each),
  long or cash, volatility-targeted, 0.35% cost per side.
* Choosing: the rule with the best Sharpe on bars up to 2024-06-30 is picked.
* Judging: the picked rule is judged once on 2024-07-01 to today, which the choice never saw,
  against holding the same coins. Every other rule's result on that window is shown too, so a
  lucky pick is visible.
* Caveat: these coins are famous today, which is hindsight; coins that died are not in the test.

Usage:  python train_memecoin.py --cache DIR   (DIR holds COIN.4h.pkl / COIN.1d.pkl; downloaded when missing)
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from loki_v8 import memecoin as mc

CHOOSE_UNTIL = "2024-06-30"
TEST_FROM = "2024-07-01"
OUT = Path(__file__).resolve().parent / "loki_v8" / "memecoin_record.json"


def download(coin: str, interval: str) -> pd.DataFrame:
    rows: list = []
    start = 0
    while True:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={coin}USDT&interval={interval}&limit=1000&startTime={start}"
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=30) as res:  # noqa: S310 - fixed public https host
                    data = json.load(res)
                break
            except (OSError, ValueError):
                time.sleep(2 + 3 * attempt)
        else:
            raise SystemExit(f"{coin} {interval}: download failed")
        if not data:
            break
        rows += data
        start = data[-1][0] + 1
        if len(data) < 1000:
            break
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "ct", "qv", "n", "tb", "tq", "x"])
    df.index = pd.to_datetime(df.t, unit="ms", utc=True)
    return df[["open", "high", "low", "close", "qv"]].astype(float)


def closes(cache: Path, interval: str, coins: list) -> Dict[str, pd.Series]:
    out = {}
    for coin in coins:
        path = cache / f"{coin}.{interval}.pkl"
        df = pd.read_pickle(path) if path.exists() else download(coin, interval)
        if not path.exists():
            df.to_pickle(path)
        out[coin] = df["close"].iloc[:-1]  # the last bar is still forming
    return out


def window(r: pd.Series, start: str = "", end: str = "") -> pd.Series:
    if start:
        r = r[r.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        r = r[r.index <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]
    return r


def walk_forward(data: Dict[str, Dict[str, pd.Series]]) -> Dict[str, Any]:
    """Re-pick the best rule every month from the 6 months before it, then judge it on that unseen month."""
    import numpy as np

    daily = {}
    for rule in mc.GRID:
        r = mc.book_returns(data[rule["bars"]], rule)
        daily[mc.label(rule)] = (1 + r).groupby(r.index.floor("D")).prod() - 1
    frame = pd.DataFrame(daily).fillna(0.0)
    hold = mc.hold_returns(data["1d"])
    hold.index = hold.index.floor("D")
    pieces, picks = [], []
    for month in pd.period_range("2021-01", pd.Timestamp.now(tz="UTC").strftime("%Y-%m"), freq="M"):
        start = month.start_time.tz_localize("UTC")
        past = frame[(frame.index < start) & (frame.index >= start - pd.DateOffset(months=6))]
        if len(past) < 100:
            continue
        best = (past.mean() / past.std() * np.sqrt(365)).idxmax()
        picks.append(best)
        pieces.append(frame[(frame.index >= start) & (frame.index < (month + 1).start_time.tz_localize("UTC"))][best])
    wf = pd.concat(pieces)
    out: Dict[str, Any] = {"how": "each month uses the rule with the best Sharpe over the 6 months before it", "periods": {}}
    for name, a, b in (("2021 to mid-2024", "2021-01-01", "2024-06-30"), ("mid-2024 on", "2024-07-01", "2100-01-01"), ("all", "2021-01-01", "2100-01-01")):
        part = wf[(wf.index >= pd.Timestamp(a, tz="UTC")) & (wf.index <= pd.Timestamp(b, tz="UTC"))]
        out["periods"][name] = {"rule": mc.stats(part, "1d"), "hold": mc.stats(hold.reindex(part.index).fillna(0.0), "1d")}
    out["most_picked"] = pd.Series(picks).value_counts().head(3).to_dict()
    return out


def build(cache: Path) -> Dict[str, Any]:
    coins = mc.COINS + mc.RESEARCH_ONLY
    data = {b: closes(cache, b, coins) for b in ("4h", "1d")}
    rows: List[Dict[str, Any]] = []
    for rule in mc.GRID:
        r = mc.book_returns(data[rule["bars"]], rule)
        rows.append({"rule": rule, "label": mc.label(rule), "choose": mc.stats(window(r, end=CHOOSE_UNTIL), rule["bars"]),
                     "test": mc.stats(window(r, start=TEST_FROM), rule["bars"]),
                     "tradable_test": mc.stats(window(mc.book_returns({c: data[rule["bars"]][c] for c in mc.COINS}, rule),
                                                      start=TEST_FROM), rule["bars"])})
    best = mc.pick({row["label"]: row["choose"].get("sharpe") for row in rows})
    chosen = next(row for row in rows if row["label"] == best)
    hold = {b: mc.hold_returns(data[b]) for b in ("4h", "1d")}
    bars = chosen["rule"]["bars"]
    test_sharpes = sorted(row["test"]["sharpe"] for row in rows)
    rank = 1 + sum(s > chosen["test"]["sharpe"] for s in test_sharpes)
    per_coin = {}
    for coin in mc.COINS:
        r = window(mc.book_returns({coin: data[bars][coin]}, chosen["rule"]), start=TEST_FROM)
        h = window(mc.hold_returns({coin: data[bars][coin]}), start=TEST_FROM)
        per_coin[coin] = {"rule": mc.stats(r, bars), "hold": mc.stats(h, bars)}
    test, hold_test = chosen["test"], mc.stats(window(hold[bars], start=TEST_FROM), bars)
    verdict = bool(test.get("sharpe", 0) > 0.5 and test.get("cagr_pct", 0) > 0 and test.get("max_dd_pct", -100) > hold_test.get("max_dd_pct", 0))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": "Binance public 4-hour and daily bars (USDT pairs), from each coin's listing",
        "coins": mc.COINS, "research_only": mc.RESEARCH_ONLY,
        "first_bar": {c: str(s.dropna().index[0].date()) for c, s in data["1d"].items()},
        "cost_per_side_pct": mc.COST * 100, "vol_target_pct": mc.VOL_TARGET * 100,
        "choose_until": CHOOSE_UNTIL, "test_from": TEST_FROM,
        "chosen": chosen, "hold": {"choose": mc.stats(window(hold[bars], end=CHOOSE_UNTIL), bars), "test": hold_test},
        "chosen_test_rank": f"{rank} of {len(rows)}", "per_coin_test": per_coin, "all_rules": rows,
        "walk_forward": walk_forward(data),
        "alpaca_spreads_pct": {"DOGE": 0.35, "SHIB": 0.24, "PEPE": 0.47, "BONK": 0.39, "WIF": 0.34, "TRUMP": 0.34, "measured": "2026-11-14"},
        "passed": verdict,
        "pass_rule": "Sharpe above 0.5 and a positive return on 2024-07 onward, with a smaller worst drop than holding the coins",
        "caveats": [
            "Simulated on past prices with assumed costs, not real trading.",
            "These coins are famous today; memecoins that collapsed to nothing are not in the test, which flatters every result.",
            "Memecoins can fall 50-90% in weeks; the rule can only step aside after a move has started.",
            "Long or cash only: Alpaca does not short crypto.",
            "Alpaca's memecoin spreads (0.24-0.47% on 14 November 2026) are wider than Binance's, so real fills there cost more than the 0.35% per side assumed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    record = build(args.cache)
    c = record["chosen"]
    print("chosen:", c["label"], "| choose", c["choose"], "| TEST", c["test"], "| rank", record["chosen_test_rank"])
    print("hold:", record["hold"])
    for row in sorted(record["all_rules"], key=lambda r: -r["choose"]["sharpe"]):
        print(f"  {row['label']:<34} choose {row['choose']['sharpe']:>6} {row['choose']['cagr_pct']:>8}%  test {row['test']['sharpe']:>6} {row['test']['cagr_pct']:>8}% dd {row['test']['max_dd_pct']}")
    print("per coin:", json.dumps(record["per_coin_test"])[:900])
    print("PASSED" if record["passed"] else "DID NOT PASS", "-", record["pass_rule"])
    if args.write:
        OUT.write_text(json.dumps(record, indent=1) + "\n")
        print("written", OUT)


if __name__ == "__main__":
    main()

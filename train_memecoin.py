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
        "passed": verdict,
        "pass_rule": "Sharpe above 0.5 and a positive return on 2024-07 onward, with a smaller worst drop than holding the coins",
        "caveats": [
            "Simulated on past prices with assumed costs, not real trading.",
            "These coins are famous today; memecoins that collapsed to nothing are not in the test, which flatters every result.",
            "Memecoins can fall 50-90% in weeks; the rule can only step aside after a move has started.",
            "Long or cash only: Alpaca does not short crypto.",
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

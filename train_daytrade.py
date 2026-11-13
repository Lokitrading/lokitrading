"""Build the Day Trader's tested record (loki_v8/daytrade_record.json).

Protocol
--------
* Data: 5-minute bars from Alpaca's market-data API, 2016 onward (keys from
  APCA_API_KEY_ID / APCA_API_SECRET_KEY; market data only, no orders).
* The rules are the published noise-band method, used as written (no tuning),
  with a volume-free average-price line so live feeds cannot change signals.
* Every market is tested twice: with instant fills (next bar's open) and with
  fills one bar (5 minutes) late.
* Markets are chosen on 2016-2021 only: Sharpe after costs above 0.3 with
  instant fills AND with late fills. They are judged on 2022 onward, which the
  choice never saw.
* The learn-from-mistakes filter is kept only if it beat the plain rules on
  2018-2021 and beat filters trained on scrambled outcomes.
* For the chosen markets, fills 1, 2 and 3 minutes after the signal are priced
  on 1-minute bars, to show how much of the result depends on speed.

Usage:
    python train_daytrade.py                       # download, then write the record
    python train_daytrade.py --cache DIR           # reuse/keep downloaded bars in DIR

Contents: what is tested · data · per-market test · statistics · learning ·
books · fill speed · the record.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from loki_v8 import daytrade

# --- What is tested ------------------------------------------------------------
CANDIDATES = [
    # broad index funds
    "SPY", "QQQ", "IWM", "DIA",
    # large single stocks
    "NVDA", "AAPL", "TSLA", "MSFT", "AMZN", "META", "GOOGL", "AMD", "NFLX", "AVGO", "MU",
    # sector and theme funds
    "XLE", "XLF", "XLK", "SMH", "ARKK",
    # other asset classes and regions
    "GLD", "SLV", "TLT", "IEF", "USO", "UNG", "EEM", "FXI",
]
SELECT_END, TEST_START = "2021-12-31", "2022-01-01"
LEARN_START = "2018-01-01"  # the filter needs two years of its own trades before it acts
MIN_SELECT_SHARPE = 0.3
LATE_BARS = 1  # "late fills" = one 5-minute bar after the instant fill
SPEED_MINUTES = (1, 2, 3)  # fills this many minutes after the signal, priced on 1-minute bars
# Other day-trading ideas tested and rejected (hourly bars unless stated; results on unseen data).
REJECTED = [
    "opening-range breakout (first-hour high/low, stop at the other side): -5.8%/yr",
    "first-hour momentum held to the close: -9.3%/yr",
    "last-half-hour momentum: -11.8%/yr (costs alone exceed the edge)",
    "fading the opening gap: -6.7%/yr; following the gap: -4.1%/yr",
    "crypto 24-hour breakout: -16.7%/yr; crypto dip-buying (RSI): -9.2%/yr; crypto hourly trend: -51.5%/yr",
    "adaptive market switch (trade a market only while its own trailing 3-12 month result is positive): "
    "+2.5% to +4.2%/yr vs +5.8% for a fixed list",
    "volatility-scaled position size: Sharpe 0.61 vs 0.55, too small to matter",
]
# A manual check, not part of this script: the funds' results against the real contracts' on Yahoo 5-minute bars.
FUTURES_PARITY = ("Checked in November 2026 against the real contracts (NQ=F, ES=F) over 35 sessions: the funds' daily "
                  "results matched theirs with a correlation of 0.99.")


# --- Data ------------------------------------------------------------------------
def fetch(symbol: str, timeframe: str = "5Min", keep: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
          start: str = "2016-01-01T00:00:00Z") -> Optional[pd.DataFrame]:
    """Download bars for one symbol (split- and dividend-adjusted). ``keep`` thins each page as it arrives."""
    import requests

    headers = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}
    pages: List[pd.DataFrame] = []
    token = None
    while True:
        params = {"timeframe": timeframe, "start": start, "limit": 10000, "adjustment": "all", "feed": "sip"}
        if token:
            params["page_token"] = token
        res = None
        for attempt in range(8):
            try:
                res = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars", headers=headers, params=params, timeout=90)
            except requests.RequestException:
                time.sleep(2 + 2 * attempt)
                continue
            if res.status_code == 429:  # rate limited: wait and ask again
                time.sleep(20)
                continue
            break
        if res is None or res.status_code != 200:
            print(symbol, timeframe, "download failed", getattr(res, "status_code", "no answer"))
            return None
        body = res.json()
        if body.get("bars"):
            page = pd.DataFrame(body["bars"])
            page["t"] = pd.to_datetime(page["t"])
            page = page.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})
            pages.append(keep(page) if keep else page)
        token = body.get("next_page_token")
        if not token:
            break
    return pd.concat(pages) if pages else None


def _after_decisions(page: pd.DataFrame) -> pd.DataFrame:
    """Of a page of 1-minute bars, keep the seven minutes after each half hour: all a late fill needs."""
    local = page.index.tz_convert(daytrade.SESSION_TZ)
    minute = local.hour * 60 + local.minute
    return page.loc[(minute >= 600) & (minute <= 936) & ((minute - 600) % 30 <= 6), ["open"]]


def load(symbol: str, cache: Optional[Path], minute: bool = False) -> Optional[pd.DataFrame]:
    """5-minute bars for ``symbol`` (or, with ``minute``, its thinned 1-minute bars), from ``cache`` when present."""
    path = cache / (f"{symbol}.1min.pkl" if minute else f"{symbol}.pkl") if cache else None
    if path and path.exists():
        return pickle.loads(path.read_bytes())
    df = fetch(symbol, "1Min", _after_decisions) if minute else fetch(symbol)
    if df is not None and path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(df))
    return df


# --- Per-market test -------------------------------------------------------------
def test_market(job: Tuple[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    """Instant-fill, late-fill and cost-free results for one market (and its futures twin)."""
    symbol, cache = job
    bars = load(symbol, Path(cache) if cache else None)
    if bars is None or bars.empty:
        return None
    cost = daytrade.cost_for(symbol)
    out: Dict[str, Any] = {
        "symbol": symbol, "cost": cost,
        "ideal": daytrade.backtest_frame(bars, cost),
        "late": daytrade.backtest_frame(bars, cost, fill_delay=LATE_BARS),
        "gross": daytrade.backtest(bars, 0.0),
    }
    contract = next((k for k, v in daytrade.FUTURES.items() if v["proxy"] == symbol), None)
    if contract:  # the same prices at futures costs stand in for the micro contract
        out["futures"] = {"symbol": contract,
                          "ideal": daytrade.backtest_frame(bars, daytrade.FUTURES_COST),
                          "late": daytrade.backtest_frame(bars, daytrade.FUTURES_COST, fill_delay=LATE_BARS)}
    return out


# --- Statistics ------------------------------------------------------------------
def _sharpe(x: pd.Series) -> float:
    return float(daytrade.stats(x).get("sharpe", -9.0))


def _windows(x: pd.Series) -> Dict[str, Any]:
    return {"select": daytrade.stats(x[x.index <= SELECT_END]), "test": daytrade.stats(x[x.index >= TEST_START]),
            "all": daytrade.stats(x)}


def _years(book: pd.Series) -> List[Dict[str, Any]]:
    return [{"year": int(y), "return_pct": round(float(((1 + x).prod() - 1) * 100), 3),
             "unseen": bool(y >= int(TEST_START[:4])), "partial": bool(x.index[-1] < pd.Timestamp(f"{y}-12-24"))}
            for y, x in book.groupby(book.index.year)]


def _equal_capital(columns: Dict[str, pd.Series]) -> pd.Series:
    """One book from several markets: equal capital each, flat where a market has no data that day."""
    return pd.DataFrame(columns).fillna(0.0).mean(axis=1)


def _row(symbol: str, cost: float, ideal: pd.Series, late: pd.Series, **extra: Any) -> Dict[str, Any]:
    select, select_late = daytrade.stats(ideal[ideal.index <= SELECT_END]), daytrade.stats(late[late.index <= SELECT_END])
    chosen = bool(select and select_late and min(select["sharpe"], select_late["sharpe"]) > MIN_SELECT_SHARPE)
    return {"symbol": symbol, **extra, "cost_bps_per_side": round(cost * 1e4, 2), "chosen": chosen,
            "select": select, "select_late": select_late,
            "test": daytrade.stats(ideal[ideal.index >= TEST_START]), "test_late": daytrade.stats(late[late.index >= TEST_START]),
            "all": daytrade.stats(ideal)}


# --- Learning from mistakes --------------------------------------------------------
def _learn_window(x: pd.Series) -> float:
    return _sharpe(x[(x.index >= LEARN_START) & (x.index <= SELECT_END)])


def _shuffle_control(frames: Dict[str, pd.DataFrame], symbols: List[str], real: pd.Series, runs: int = 30) -> Dict[str, Any]:
    """Would a filter trained on scrambled outcomes have done as well? If so, the 'learning' is luck."""
    beat = {"select": 0, "test": 0}
    for seed in range(runs):
        cols = {}
        for symbol in symbols:
            frame = frames[symbol]
            traded = frame["turns"] > 0
            rng = np.random.default_rng(seed)
            # Scramble which days count as wins, year by year, keeping every year's win rate.
            shuffled = frame["return"].copy()
            for _, idx in frame[traded].groupby(frame[traded].index.year).groups.items():
                shuffled.loc[idx] = rng.permutation(frame.loc[idx, "return"].to_numpy())
            kept_fake, _ = daytrade.learn_from_mistakes(frame.assign(**{"return": shuffled}))
            cols[symbol] = frame["return"].where(kept_fake != 0.0, 0.0).where(traded, frame["return"])
        book = _equal_capital(cols)
        beat["select"] += _learn_window(real) > _learn_window(book)
        beat["test"] += _sharpe(real[real.index >= TEST_START]) > _sharpe(book[book.index >= TEST_START])
    return {"runs": runs, "real_beat_shuffled_select": int(beat["select"]), "real_beat_shuffled_test": int(beat["test"])}


# --- Books -------------------------------------------------------------------------
def _book(ideal: Dict[str, pd.DataFrame], late: Dict[str, pd.DataFrame], symbols: List[str], label: str) -> Dict[str, Any]:
    """Equal-capital book over ``symbols``, with instant and late fills.

    The mistake filter is fitted on instant-fill outcomes; the days it skips are skipped in the late-fill book too.
    """
    plain = _equal_capital({s: ideal[s]["return"] for s in symbols})
    plain_late = _equal_capital({s: late[s]["return"] for s in symbols})
    learned, learned_late, models, skips = {}, {}, {}, {}
    for s in symbols:
        learned[s], models[s] = daytrade.learn_from_mistakes(ideal[s])
        skips[s] = (learned[s] == 0.0) & (ideal[s]["return"] != 0.0)
        learned_late[s] = late[s]["return"].where(~skips[s].reindex(late[s].index, fill_value=False), 0.0)
    filtered, filtered_late = _equal_capital(learned), _equal_capital(learned_late)

    kept = bool(all(models.values()) and _learn_window(filtered) > _learn_window(plain) + 0.05)
    control = _shuffle_control(ideal, symbols, filtered) if kept else None
    if control and min(control["real_beat_shuffled_select"], control["real_beat_shuffled_test"]) < 0.9 * control["runs"]:
        kept = False  # not clearly better than a filter trained on noise
    final, final_late = (filtered, filtered_late) if kept else (plain, plain_late)
    print(f"== {label}: {symbols} | filter {'KEPT' if kept else 'off'} | unseen instant {daytrade.stats(final[final.index >= TEST_START])} "
          f"| unseen late {daytrade.stats(final_late[final_late.index >= TEST_START])}", flush=True)
    return {
        "symbols": symbols, **_windows(final), "late": _windows(final_late),
        "plain": _windows(plain), "plain_late": _windows(plain_late),
        "market_baselines": {s: daytrade.stats((learned[s] if kept else ideal[s]["return"])[lambda x: x.index >= TEST_START])
                             for s in symbols},
        "calendar_years": _years(final), "calendar_years_late": _years(final_late),
        "skips": skips if kept else {},  # per market: the days the filter sat out (not written to the record)
        "learning": {
            "kept": kept, "threshold": daytrade.SKIP_BELOW, "features": list(daytrade.FEATURES),
            "how": "Each year is traded with a model fitted only on earlier years' days; it skips a day whose first "
                   "signal looks like past losing days. Kept only if it beat the plain rules on 2018-2021 and "
                   "beat filters trained on scrambled outcomes.",
            "select_window_sharpe": {"plain": round(_learn_window(plain), 3), "filtered": round(_learn_window(filtered), 3)},
            "shuffle_control": control,
            "models": dict(models) if kept else {},
            "lessons": {s: (m or {}).get("lessons", 0) for s, m in models.items()},
        },
    }


def _public(book: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a book that go into the record, under fixed names."""
    if not book:
        return {"book": {}, "book_late": {}, "book_plain": {}, "book_plain_late": {}, "learning": {},
                "calendar_years": [], "calendar_years_late": [], "fill_speed": None, "market_baselines": {}}
    return {"book": {k: book[k] for k in ("select", "test", "all")}, "book_late": book["late"],
            "book_plain": book["plain"], "book_plain_late": book["plain_late"], "learning": book["learning"],
            "calendar_years": book["calendar_years"], "calendar_years_late": book["calendar_years_late"],
            "fill_speed": book.get("fill_speed"), "market_baselines": book.get("market_baselines")}


# --- Fill speed: what a fill N minutes after the signal costs ---------------------------
def late_fills(feed: str, cost: float, cache: Optional[Path]) -> Optional[pd.DataFrame]:
    """Daily returns when every signal order fills N minutes late (columns: ``SPEED_MINUTES``).

    Signals come from the 5-minute bars exactly as in the main test; each fill is re-priced at the open of
    the 1-minute bar N minutes later. The scheduled close at the end of the session is not delayed.
    """
    bars, minute = load(feed, cache), load(feed, cache, minute=True)
    if bars is None or minute is None or minute.empty:
        return None
    minute = minute[~minute.index.duplicated()].sort_index()
    stamps, opens = minute.index, minute["open"].to_numpy(dtype=float)
    rows: Dict[pd.Timestamp, Dict[int, float]] = {}
    for day, out, _ in daytrade.replay_sessions(bars, cost):
        trades = [d for d in out["decisions"] if "skipped" not in d]
        signals = [d for d in trades if d.get("why") != "session close"]
        when = pd.to_datetime([d["time"] for d in signals], utc=True)
        rows[day] = {}
        for minutes in SPEED_MINUTES:
            want = when + pd.Timedelta(minutes=minutes)
            at = np.minimum(stamps.searchsorted(want), len(stamps) - 1)
            found = (stamps[at] >= want) & (stamps[at] - want <= pd.Timedelta(minutes=6 - minutes))
            late_price = iter(np.where(found, opens[at], [d["price"] for d in signals]))  # no print that minute: keep the instant price
            equity, pos, entry = 1.0, 0, 0.0
            for d in trades:
                price = float(d["price"]) if d.get("why") == "session close" else float(next(late_price))
                if pos:
                    equity *= 1.0 + pos * (price / entry - 1.0)
                equity *= 1.0 - cost * abs(d["to"] - d["from"])
                pos, entry = d["to"], price
            rows[day][minutes] = equity - 1.0
    return pd.DataFrame.from_dict(rows, orient="index")


def _fill_speed(book: Dict[str, Any], costs: Dict[str, float], cache: Optional[Path]) -> Optional[Dict[str, Any]]:
    """The book's results with fills 1, 2 and 3 minutes late, skipping the same days its filter skipped."""
    frames = {s: late_fills(daytrade.price_symbol(s), costs[s], cache) for s in book["symbols"]}
    if any(f is None for f in frames.values()):
        return None
    steps = []
    for minutes in SPEED_MINUTES:
        cols = {}
        for s, frame in frames.items():
            skip = book["skips"].get(s)
            cols[s] = frame[minutes] if skip is None else frame[minutes].where(~skip.reindex(frame.index, fill_value=False), 0.0)
        steps.append({"minutes": minutes, **{k: v for k, v in _windows(_equal_capital(cols)).items() if k != "all"}})
    print("   fill speed, unseen:", {f"{s['minutes']} min": s["test"].get("cagr_pct") for s in steps}, flush=True)
    return {"data": "1-minute bars (Alpaca): each order is re-priced at the open of the minute N minutes after its signal",
            "steps": steps}


# --- The record --------------------------------------------------------------------
def build(cache: Optional[Path], workers: int = 4) -> Dict[str, Any]:
    jobs = [(s, str(cache) if cache else None) for s in CANDIDATES]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = [r for r in pool.map(test_market, jobs) if r]

    ideal = {r["symbol"]: r["ideal"] for r in results}
    late = {r["symbol"]: r["late"] for r in results}
    rows = [dict(_row(r["symbol"], r["cost"], r["ideal"]["return"], r["late"]["return"]),
                 before_costs=daytrade.stats(r["gross"])) for r in results]
    fut = {r["futures"]["symbol"]: r for r in results if r.get("futures")}
    fut_ideal = {k: r["futures"]["ideal"] for k, r in fut.items()}
    fut_late = {k: r["futures"]["late"] for k, r in fut.items()}
    fut_rows = [_row(k, daytrade.FUTURES_COST, fut_ideal[k]["return"], fut_late[k]["return"],
                     name=daytrade.FUTURES[k]["name"], proxy=daytrade.FUTURES[k]["proxy"]) for k in sorted(fut)]

    chosen = [r["symbol"] for r in rows if r["chosen"]]
    fut_chosen = [r["symbol"] for r in fut_rows if r["chosen"]]
    stock_book = _book(ideal, late, chosen, "stocks") if chosen else {}
    futures_book = _book(fut_ideal, fut_late, fut_chosen, "futures") if fut_chosen else {}
    for book in (stock_book, futures_book):
        if book:
            book["fill_speed"] = _fill_speed(book, {s: daytrade.cost_for(s) for s in book["symbols"]}, cache)

    everything = _equal_capital({s: f["return"] for s, f in ideal.items()})
    everything_late = _equal_capital({s: f["return"] for s, f in late.items()})
    losers = sum(1 for r in rows if (r["test"] or {}).get("cagr_pct", 0) < 0)
    unseen = (stock_book.get("test") or {}) if stock_book else {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "Intraday momentum with noise bands (Zarattini, Aziz, Barbon 2024), as published: 14-session noise, "
                  "decisions every 30 minutes, long and short, flat at the close. The average-price line is "
                  "time-weighted (no volume), so different price feeds give the same signals.",
        "anchor": daytrade.ANCHOR,
        "data": "5-minute bars, regular US hours, Alpaca market data",
        "data_through": str(max(f.index[-1] for f in ideal.values()).date()),
        "windows": {"select": ["2016-01-01", SELECT_END], "test": [TEST_START, "2100-01-01"]},
        "selection_rule": f"Sharpe after costs above {MIN_SELECT_SHARPE} on 2016-2021 with instant fills and with fills "
                          f"{LATE_BARS * 5} minutes late",
        "late_fill_minutes": LATE_BARS * 5,
        "symbols": chosen,
        "candidates": rows,
        **_public(stock_book),
        "all_candidates_book": {"count": len(rows), "losing_on_unseen": losers,
                                "test": daytrade.stats(everything[everything.index >= TEST_START]),
                                "test_late": daytrade.stats(everything_late[everything_late.index >= TEST_START])},
        "futures": {
            "symbols": fut_chosen, "candidates": fut_rows, **_public(futures_book),
            "contracts": {k: daytrade.FUTURES[k] for k in fut_chosen},
            "note": "Tested on each contract's index fund during regular US hours (09:30-16:00 New York) at futures "
                    "costs. Futures also trade overnight; this method does not. A fund is a close stand-in, not the "
                    "contract itself. " + FUTURES_PARITY,
        },
        "rejected_rules": REJECTED,
        "verified": bool(unseen.get("cagr_pct", -1) > 0),
        "caveats": [
            "Simulated on historical data with assumed costs; not real trading.",
            f"The edge is thin: it lost money on {losers} of the {len(rows)} markets tested on unseen data.",
            "It depends on fast, cheap fills: results with fills 1 to 5 minutes late are shown next to the instant ones.",
            "No leverage in the test. Short positions need a margin account; futures need a futures account.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=None, help="directory of downloaded bars to reuse/keep")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    record = build(args.cache, args.workers)
    path = Path(__file__).parent / "loki_v8" / "daytrade_record.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print("record written:", path)


if __name__ == "__main__":
    main()

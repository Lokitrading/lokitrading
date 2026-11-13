"""The Day Trader's weekly self-review: walk-forward search, shadow twin, human promotion.

Run every Sunday by .github/workflows/daytrade-evolution.yml (or by hand):

    python evolve_daytrade.py --cache DIR            # 5-minute bars (downloaded from Alpaca if missing)
    python evolve_daytrade.py --cache DIR --write    # store the result in loki_v8/daytrade_evolution.json
    python evolve_daytrade.py --summary review.md    # also write a human-readable report

Three gates, in order. A new setting reaches the live site only through all three:

1. Walk-forward search. Nine settings are tried: the noise lookback (10, 14 or 20 sessions)
   times the band width (0.8, 1.0 or 1.2 x the noise); the published rules are 14 and 1.0.
   From 2017 on, each month is traded with whichever setting had the best Sharpe ratio over
   the previous 6 months, then judged on that month, which it had not seen. The process
   passes only if, on those unseen months, it beats the current rules' Sharpe by at least
   0.10 overall AND is no worse over the last 24 months. If it passes and its current pick
   differs from the current rules, that pick becomes the shadow candidate.
2. Shadow twin. The candidate is replayed on live 5-minute prices from the day it was
   picked, beside the current rules, with no money. After at least 7 completed sessions it
   passes only if its average day is positive AND at least as good as the current rules'.
3. A person. A candidate that passes the shadow test is proposed as a pull request; only
   merging it changes the live rules (and the record is rebuilt with the new setting).

Settings are compared on the plain rules (no mistake filter) on both sides, so the filter
cannot favour one of them. Every generation, promoted or not, is kept in the log.
"""

from __future__ import annotations

import argparse
import json
import math
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from loki_v8 import daytrade

ROOT = Path(__file__).parent
RECORD = ROOT / "loki_v8" / "daytrade_record.json"
STATE = ROOT / "loki_v8" / "daytrade_evolution.json"
TRAIN_MONTHS = 6
FIRST_TEST_MONTH = "2017-01"
SHARPE_MARGIN = 0.10
RECENT_MONTHS = 24
SHADOW_SESSIONS = 7
CONTENDERS = [{"lookback": lb, "band": bd} for lb, bd in product(daytrade.PARAM_GRID["lookback"], daytrade.PARAM_GRID["band"])]


def _key(params: Dict[str, Any]) -> str:
    lookback, band = daytrade.settings(params)
    return f"lookback {lookback}, band {band:g}"


def _sharpe(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / x.std() * math.sqrt(252)) if len(x) > 20 and float(x.std()) > 0 else float("nan")


# --- Gate 1: walk-forward search ------------------------------------------------------------------
def _daily(job: Tuple[str, str, Dict[str, Any]]) -> Tuple[str, str, pd.Series]:
    symbol, cache, params = job
    import train_daytrade

    bars = train_daytrade.load(symbol, Path(cache))
    if bars is None or bars.empty:
        return symbol, _key(params), pd.Series(dtype=float)
    return symbol, _key(params), daytrade.backtest_frame(bars, daytrade.cost_for(symbol), params=params)["return"]


def books(symbols: List[str], cache: Path, workers: int = 4) -> Dict[str, pd.Series]:
    """Each setting's equal-capital daily book over ``symbols`` (plain rules, after costs)."""
    jobs = [(s, str(cache), p) for p in CONTENDERS for s in symbols]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_daily, jobs))
    cols: Dict[str, Dict[str, pd.Series]] = {}
    for symbol, key, series in results:
        cols.setdefault(key, {})[symbol] = series
    return {key: pd.DataFrame(per).fillna(0.0).mean(axis=1) for key, per in cols.items()}


def walk_forward(book: Dict[str, pd.Series], champion: str) -> Dict[str, Any]:
    """Trade each month with the setting that was best over the previous TRAIN_MONTHS; judge it on that month."""
    frame = pd.DataFrame(book).dropna(how="all").fillna(0.0)
    months = frame.index.to_period("M")
    picks, chosen = [], []
    for month in sorted(set(months)):
        if str(month) < FIRST_TEST_MONTH:
            continue
        train = frame[(months >= month - TRAIN_MONTHS) & (months < month)]
        if len(train) < 60:
            continue
        scores = {k: _sharpe(train[k]) for k in frame.columns}
        best = max(scores, key=lambda k: (scores[k] if not math.isnan(scores[k]) else -9.0, k == champion))
        picks.append({"month": str(month), "pick": best})
        chosen.append(frame.loc[months == month, best])
    adaptive = pd.concat(chosen) if chosen else pd.Series(dtype=float)
    fixed = frame[champion].reindex(adaptive.index)
    cut = adaptive.index[-1] - pd.DateOffset(months=RECENT_MONTHS) if len(adaptive) else None
    result = {
        "months_tested": len(picks),
        "adaptive_sharpe": round(_sharpe(adaptive), 3), "champion_sharpe": round(_sharpe(fixed), 3),
        "adaptive_recent_sharpe": round(_sharpe(adaptive[adaptive.index > cut]), 3) if cut is not None else None,
        "champion_recent_sharpe": round(_sharpe(fixed[fixed.index > cut]), 3) if cut is not None else None,
        "months_on_champion_pct": round(100 * sum(p["pick"] == champion for p in picks) / max(1, len(picks)), 1),
        "current_pick": picks[-1]["pick"] if picks else champion,
        "fixed_settings": {k: {"sharpe_2016_21": round(_sharpe(v[v.index <= "2021-12-31"]), 3),
                               "sharpe_2022_on": round(_sharpe(v[v.index >= "2022-01-01"]), 3)} for k, v in frame.items()},
    }
    result["passed"] = bool(
        result["adaptive_sharpe"] >= result["champion_sharpe"] + SHARPE_MARGIN
        and result["adaptive_recent_sharpe"] is not None and result["champion_recent_sharpe"] is not None
        and result["adaptive_recent_sharpe"] >= result["champion_recent_sharpe"])
    return result


def _why_not(wf: Dict[str, Any]) -> str:
    reasons = []
    if not wf["adaptive_sharpe"] >= wf["champion_sharpe"] + SHARPE_MARGIN:
        reasons.append(f"unseen-month Sharpe {wf['adaptive_sharpe']} vs {wf['champion_sharpe']}, needs +{SHARPE_MARGIN}")
    if not (wf["adaptive_recent_sharpe"] is not None and wf["champion_recent_sharpe"] is not None
            and wf["adaptive_recent_sharpe"] >= wf["champion_recent_sharpe"]):
        reasons.append(f"worse over the last {RECENT_MONTHS} months: {wf['adaptive_recent_sharpe']} vs {wf['champion_recent_sharpe']}")
    return "; ".join(reasons)


# --- Gate 2: shadow twin --------------------------------------------------------------------------
def shadow(symbols: List[str], candidate: Dict[str, Any], champion: Dict[str, Any], since: str,
           bars_for=None) -> Dict[str, Any]:
    """The candidate and the current rules replayed side by side on live bars since ``since``."""
    if bars_for is None:
        from loki_v8.data import get_provider

        provider = get_provider()
        bars_for = lambda s: provider.fetch_history(daytrade.price_symbol(s), "60d", "5m")  # noqa: E731
    days: Dict[str, Dict[str, float]] = {"candidate": {}, "champion": {}}
    per: Dict[str, Dict[str, Dict[str, float]]] = {"candidate": {}, "champion": {}}
    for symbol in symbols:
        bars = bars_for(symbol)
        if bars is None or bars.empty:
            continue
        for name, params in (("candidate", candidate), ("champion", champion)):
            per[name][symbol] = daytrade.forward_record(bars, symbol, since, params=params)
    out: Dict[str, Any] = {}
    for name in ("candidate", "champion"):
        book = daytrade.summarize_forward(per[name], len(symbols))
        daily = [d["return_pct"] for d in book.get("daily", [])]
        days[name] = {d["day"]: d["return_pct"] for d in book.get("daily", [])}
        out[name] = {"sessions": book.get("days", 0), "mean_day_pct": round(sum(daily) / len(daily), 4) if daily else None,
                     "total_pct": book.get("return_pct")}
    sessions = out["candidate"]["sessions"]
    cand, champ = out["candidate"]["mean_day_pct"], out["champion"]["mean_day_pct"]
    out["sessions"] = sessions
    out["ready"] = sessions >= SHADOW_SESSIONS
    out["passed"] = bool(out["ready"] and cand is not None and champ is not None and cand > 0 and cand >= champ)
    return out


# --- One generation ----------------------------------------------------------------------------------
def generation(state: Dict[str, Any], record: Dict[str, Any], cache: Optional[Path], workers: int,
               bars_for=None) -> Dict[str, Any]:
    symbols = list(record.get("symbols") or [])
    champion = record.get("params") or {"lookback": daytrade.LOOKBACK, "band": daytrade.BAND}
    gen: Dict[str, Any] = {"generation": len(state.get("generations", [])) + 1,
                           "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "champion": _key(champion), "outcome": "kept the current rules"}
    pending = state.get("shadow")
    if pending:
        result = shadow(symbols, pending["params"], champion, pending["since"], bars_for)
        gen["shadow"] = {**{k: v for k, v in result.items() if k != "candidate"}, "candidate_result": result["candidate"],
                         "candidate": _key(pending["params"]), "since": pending["since"]}
        if not result["ready"]:
            gen["outcome"] = f"shadow test continues ({result['sessions']} of {SHADOW_SESSIONS} sessions)"
        elif result["passed"]:
            state["promote"] = {**pending, "shadow": result}
            state["shadow"] = None
            gen["outcome"] = f"shadow test passed: {_key(pending['params'])} is proposed for promotion (needs a person to merge)"
        else:
            state["shadow"] = None
            gen["outcome"] = f"shadow test failed: {_key(pending['params'])} rejected, current rules kept"
    elif cache is not None:
        book = books(symbols, cache, workers)
        wf = walk_forward(book, _key(champion))
        gen["walk_forward"] = wf
        if wf["passed"] and wf["current_pick"] != _key(champion):
            params = next(p for p in CONTENDERS if _key(p) == wf["current_pick"])
            state["shadow"] = {"params": params, "since": str(record.get("data_through") or gen["run_at"][:10]),
                               "picked_at": gen["run_at"]}
            gen["outcome"] = f"walk-forward passed: {wf['current_pick']} enters the shadow test"
        elif wf["passed"]:
            gen["outcome"] = "walk-forward passed, and its current pick is the current rules: nothing to change"
        else:
            gen["outcome"] = f"walk-forward did not pass ({_why_not(wf)}): current rules kept"
    else:
        gen["outcome"] = "no 5-minute history available this week (no cache and no Alpaca keys): nothing tested"
    state.setdefault("generations", []).append(gen)
    state["generations"] = state["generations"][-52:]
    state["champion"] = champion
    return gen


def summary(gen: Dict[str, Any]) -> str:
    lines = [f"## Day Trader self-review, generation {gen['generation']}", "", f"**Outcome:** {gen['outcome']}", "",
             f"Current rules: {gen['champion']}."]
    wf = gen.get("walk_forward")
    if wf:
        lines += ["", "### Walk-forward (6 months chosen, 1 unseen month judged, from 2017)", "",
                  f"- Months tested: {wf['months_tested']}; months on the current rules: {wf['months_on_champion_pct']}%",
                  f"- Sharpe on unseen months: adaptive {wf['adaptive_sharpe']} vs current {wf['champion_sharpe']} (needs +{SHARPE_MARGIN})",
                  f"- Last {RECENT_MONTHS} months: adaptive {wf['adaptive_recent_sharpe']} vs current {wf['champion_recent_sharpe']}",
                  "", "| Setting | Sharpe 2016-21 | Sharpe 2022 on |", "|---|---|---|"]
        lines += [f"| {k} | {v['sharpe_2016_21']} | {v['sharpe_2022_on']} |" for k, v in sorted(wf["fixed_settings"].items())]
    sh = gen.get("shadow")
    if sh:
        cand, champ = sh["candidate_result"], sh["champion"]
        lines += ["", f"### Shadow test of {sh['candidate']} since {sh['since']}", "",
                  f"- Sessions: {sh['sessions']} (needs {SHADOW_SESSIONS})",
                  f"- Average day: candidate {cand['mean_day_pct']}% vs current rules {champ['mean_day_pct']}%",
                  f"- Total: candidate {cand['total_pct']}% vs current rules {champ['total_pct']}%"]
    lines += ["", "Simulated on historical and live prices, not real trading. Nothing changes until a person merges."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=None, help="directory of 5-minute bars (train_daytrade.py format)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--write", action="store_true", help="store the result in loki_v8/daytrade_evolution.json")
    parser.add_argument("--summary", type=Path, default=None, help="write a markdown report here")
    args = parser.parse_args()
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"generations": []}
    gen = generation(state, record, args.cache, args.workers)
    print(gen["outcome"])
    if args.write and state.get("promote") and args.cache is not None:
        # A candidate passed the shadow test: rebuild the whole record with its setting. The change is
        # proposed as a pull request by the workflow; it reaches the live site only if a person merges it.
        import train_daytrade

        promoted = state.pop("promote")
        new_record = train_daytrade.build(args.cache, args.workers, promoted["params"])
        RECORD.write_text(json.dumps(new_record, indent=1), encoding="utf-8")
        state["champion"] = new_record["params"]
        gen["outcome"] += f"; record rebuilt with {_key(promoted['params'])}"
        print("record rebuilt with", _key(promoted["params"]))
    if args.write:
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    if args.summary:
        args.summary.write_text(summary(gen), encoding="utf-8")


if __name__ == "__main__":
    main()

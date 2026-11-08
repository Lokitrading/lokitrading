"""One generation of Loki's self-evolution: champion vs challengers, then learn from new trades.

Run weekly by .github/workflows/ai-evolution.yml (or by hand):

    python evolve.py                              # fetch history via yfinance
    python evolve.py --data-dir state/history     # reuse cached CSVs
    python evolve.py --write-snapshot             # store the result for the live AI
    python evolve.py --summary evolution.md       # write a human-readable report

Rules (see loki_v8/evolution.py):

* The live profile is the champion. Its one-step neighbours on the training
  grid and a few random grid points are the challengers.
* Every contender is backtested over the full history with the same costs,
  windows and scoring the champion was originally chosen with.
* A challenger is promoted only if it beats the champion's rank score
  (the worse of development and validation) by a clear margin AND is not
  worse on 2023-onward or the last 12 months. Recent data can only veto.
* The learned-from-mistakes filter is re-validated walk-forward, and when it
  is kept it is refitted on every closed trade so it learns from the newest.
* Every generation, promoted or not, is appended to loki_v8/evolution_log.json.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import train_long_history as tlh
from loki_v8 import evolution, mistake_model

ROOT = Path(__file__).parent
SNAPSHOT = ROOT / "loki_v8" / "training_report_snapshot.json"
LOG_PATH = ROOT / "loki_v8" / "evolution_log.json"
UNIVERSES = {"equity": tlh.EQUITY_UNIVERSE, "crypto": tlh.CRYPTO_UNIVERSE}


def _init_worker(data, recent_start: str) -> None:
    tlh._init(data)
    for kind in tlh.WINDOWS:
        tlh.WINDOWS[kind]["recent"] = (recent_start, "2100-01-01")


def _scores(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    dev, val = tlh.score(rows, "development"), tlh.score(rows, "validation")
    return {"development_score": round(dev, 3), "validation_score": round(val, 3), "rank_score": round(min(dev, val), 3),
            "holdout_score": round(tlh.score(rows, "holdout"), 3), "recent_score": round(tlh.score(rows, "recent"), 3)}


def evaluate_all(kind: str, data, contenders: List[Dict[str, Any]], workers: int, recent_start: str) -> List[Dict[str, Any]]:
    symbols = list(data)
    jobs = [(c, s, kind) for c in contenders for s in symbols]
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(data, recent_start)) as pool:
        results = list(pool.map(tlh.evaluate, jobs, chunksize=max(1, len(jobs) // (workers * 6))))
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_key.setdefault(json.dumps(r["params"], sort_keys=True), []).append(r)
    out = []
    for c in contenders:
        rows = by_key.get(json.dumps(c, sort_keys=True), [])
        out.append({"params": c, **_scores(rows), "rows": rows})
    return out


def full_params(grid: Dict[str, Any]) -> Dict[str, Any]:
    return {**tlh.FIXED, **{k: v for k, v in grid.items() if k != "preset"},
            "factor_preset": grid["preset"], "factor_weights": tlh.PRESETS[grid["preset"]]}


def refit_on_all_trades(kind: str, data, params: Dict[str, Any], meta: Dict[str, Any], data_through: str) -> Tuple[Dict[str, Any], int]:
    """Validated filter → refit on every closed trade so it learns from the newest ones."""
    bpy = 365 if kind == "crypto" else 252
    replay = tlh._run_all(data, kind, params)
    frames = [b["frame"] for b in replay.values()]
    trades = [b["trades"] for b in replay.values()]
    X, y = mistake_model.training_rows(frames, trades, bpy)
    if len(y) < 60 or y.min() == y.max():
        return meta, int(len(y))
    model = mistake_model.fit(X, y)
    return {"model": model, "threshold": meta["threshold"], "trained_on_trades_closed_before": data_through,
            "validated_with_trades_closed_before": meta.get("trained_on_trades_closed_before")}, int(len(y))


def _short(params: Dict[str, Any]) -> str:
    return (f"{params['preset']} · entry bar {params['signal_threshold']:g} · stop {params['stop_loss_atr']:g} ATR · "
            f"trail {params['trailing_stop_atr']:g} · vol target {params['target_vol_pct']:g}% · exit on {params['exit_mode']}")


def evolve_kind(kind: str, snapshot_block: Dict[str, Any], data_dir: Optional[Path], workers: int,
                n_random: int, seed: int, recent_start: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Returns (report block for write_snapshot, log entry)."""
    started = time.time()
    data = {s: tlh.load_history(s, data_dir) for s in UNIVERSES[kind]}
    data_through = max(str(df.index[-1].date()) for df in data.values())
    champion_grid = evolution.grid_params(snapshot_block["params"])
    rivals = evolution.challengers(champion_grid, tlh.GRID, n_random=n_random, seed=seed)
    results = evaluate_all(kind, data, [champion_grid, *rivals], workers, recent_start)
    champion, contenders = results[0], results[1:]
    decision = evolution.decide(champion, contenders)
    winner = decision["winner"]
    params = full_params(winner["params"])

    mistakes = tlh.learn_from_mistakes(kind, data, params)
    incumbent = (snapshot_block["params"].get("meta_filter") or {}).get("threshold")
    if not mistakes.get("kept") and incumbent is not None and mistakes.get("threshold") != incumbent:
        # Champion vs challenger for the filter too: the setting already live is
        # re-checked on its own before it can be dropped, so one week's noisy
        # threshold search cannot switch a working filter off.
        retry = tlh.learn_from_mistakes(kind, data, params, thresholds=(float(incumbent),))
        if retry.get("kept"):
            retry["reason"] = (f"the live setting ({incumbent:g}) passed its checks again; the new search's pick "
                               f"({mistakes.get('threshold')}) did not hold up on unseen data")
            mistakes = retry
    holdout = [{"symbol": r["symbol"], "strategy": r.get("holdout") or {}, "buy_hold": r.get("holdout_bh") or {},
                "development": r.get("development"), "validation": r.get("validation")} for r in winner["rows"]]
    pooled = tlh._pooled(holdout)
    lessons = None
    if mistakes.get("kept"):
        deploy_meta, lessons = refit_on_all_trades(kind, data, params, mistakes["meta"], data_through)
        params["meta_filter"] = deploy_meta
        holdout, pooled = mistakes["holdout"], mistakes["holdout_pooled"]  # honest: judged with the pre-2023 fit
    windows = {k: v for k, v in tlh.WINDOWS[kind].items() if k != "recent"}
    report = {"best_params": params, "symbols": UNIVERSES[kind], "windows": windows,
              "development_score": winner["development_score"], "validation_score": winner["validation_score"],
              "holdout_pooled": pooled, "holdout": holdout, "data_through": data_through,
              "learning_from_mistakes": {k: v for k, v in mistakes.items() if k not in ("meta", "holdout")}}

    def card(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not entry:
            return None
        return {"params": entry["params"], "label": _short(entry["params"]),
                **{k: entry[k] for k in ("development_score", "validation_score", "rank_score", "holdout_score", "recent_score")}}

    log = {"promoted": decision["promoted"], "reason": decision["reason"], "champion": card(champion),
           "best_challenger": card(decision["best_challenger"]), "winner": card(winner),
           "challengers_tested": len(contenders), "data_through": data_through,
           "holdout_pooled": {k: pooled.get(k) for k in ("mean_cagr_pct", "mean_sharpe", "mean_max_dd_pct", "profitable_symbols", "symbols")},
           "mistakes": {"kept": bool(mistakes.get("kept")), "threshold": mistakes.get("threshold"),
                        "lessons": lessons, "reason": mistakes.get("reason")},
           "seconds": round(time.time() - started, 1)}
    return report, log


def markdown(entry: Dict[str, Any]) -> str:
    lines = [f"## Loki AI evolution — generation {entry['generation']} ({entry['date']})", "",
             f"Data through {entry['data_through']}. All results are hypothetical backtests with fees and slippage.", ""]
    for kind, title in (("equity", "Stocks/ETFs"), ("crypto", "Crypto")):
        k = entry[kind]
        lines += [f"### {title}: {'🧬 PROMOTED a challenger' if k['promoted'] else 'champion kept'}",
                  f"- Decision: {k['reason']}",
                  f"- Champion: {k['champion']['label']} — rank {k['champion']['rank_score']}, 2023+ {k['champion']['holdout_score']}, last 12m {k['champion']['recent_score']}"]
        if k.get("best_challenger"):
            b = k["best_challenger"]
            lines.append(f"- Best of {k['challengers_tested']} challengers: {b['label']} — rank {b['rank_score']}, 2023+ {b['holdout_score']}, last 12m {b['recent_score']}")
        m = k["mistakes"]
        lines.append(f"- Learn-from-mistakes filter: {'kept, refitted on ' + str(m['lessons']) + ' closed trades' if m['kept'] else 'off — ' + str(m['reason'])}")
        hp = k["holdout_pooled"]
        lines.append(f"- 2023-onward (hypothetical): {hp.get('mean_cagr_pct')}%/yr average, Sharpe {hp.get('mean_sharpe')}, "
                     f"profitable on {hp.get('profitable_symbols')}/{hp.get('symbols')} symbols")
        lines.append("")
    p = entry.get("portfolio") or {}
    if p:
        lines.append(f"### Portfolio AI\n- 2023-onward (hypothetical): {p.get('cagr_pct')}%/yr, Sharpe {p.get('sharpe')}, worst drop {p.get('max_dd_pct')}% · verified: {p.get('verified')}")
    lines += ["", "Merging this PR deploys the new snapshot to the live site. Close it to keep the current AI."]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("state/history"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--random", type=int, default=6, help="random grid challengers per kind on top of the neighbours")
    parser.add_argument("--write-snapshot", action="store_true")
    parser.add_argument("--summary", type=Path, default=None, help="write a markdown report of this generation")
    parser.add_argument("--skip-portfolio", action="store_true", help="for quick local runs only")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    seed = int(now.strftime("%G%V"))  # one seed per ISO week: reruns in the same week test the same challengers
    recent_start = str((now - timedelta(days=365)).date())
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    universal = snapshot.get("universal") or {}
    report: Dict[str, Any] = {"generated_at": now.isoformat(timespec="seconds"), "protocol": universal.get("protocol") or
                              "universal profile; select on min(development, validation); holdout read once for reporting",
                              "fees_bps_per_side": tlh.FEE_BPS, "slippage_bps_per_side": tlh.FIXED["slippage_bps"]}
    entry: Dict[str, Any] = {"date": str(now.date())}
    for kind in ("equity", "crypto"):
        report[kind], entry[kind] = evolve_kind(kind, universal[kind], args.data_dir, args.workers, args.random, seed, recent_start)
        print(f"== {kind}: {'PROMOTED' if entry[kind]['promoted'] else 'kept'} — {entry[kind]['reason']} ({entry[kind]['seconds']}s)")
    entry["data_through"] = max(report[k]["data_through"] for k in ("equity", "crypto"))
    if not args.skip_portfolio:
        report["portfolio"] = tlh.portfolio_stage(report, args.data_dir)
        hold = report["portfolio"].get("holdout") or {}
        entry["portfolio"] = {k: hold.get(k) for k in ("cagr_pct", "sharpe", "max_dd_pct")} | {"verified": report["portfolio"].get("verified")}
    elif universal.get("portfolio"):
        report["portfolio"] = universal["portfolio"]

    if args.write_snapshot:
        tlh.write_snapshot(report)
        log = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else {}
        log = evolution.append_log(log, entry)
        LOG_PATH.write_text(json.dumps(log, indent=1, default=str) + "\n", encoding="utf-8")
        entry = log["generations"][-1]
    else:
        entry["generation"] = "dry-run"
    text = markdown(entry)
    print("\n" + text)
    if args.summary:
        args.summary.write_text(text + "\n", encoding="utf-8")
    if any(not math.isfinite(float(entry[k]["winner"]["rank_score"])) for k in ("equity", "crypto")):
        raise SystemExit("non-finite score — refusing to continue")


if __name__ == "__main__":
    main()

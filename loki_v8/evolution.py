"""Self-evolution for Loki's universal trading profiles: champion vs challengers.

Every generation (run weekly by ``evolve.py``):

1. The current profile is the **champion**. Challengers are its one-step
   neighbours on the training grid plus a few random grid points.
2. Champion and challengers are re-tested on the latest data with exactly the
   same protocol the champion was chosen with (rank = the worse of the
   development and validation scores).
3. A challenger is promoted only if it beats the champion's rank score by a
   clear margin AND is no worse on the recent windows. Recent data can veto a
   promotion but never choose one, so it cannot be fitted to.
4. Whatever wins, the learned-from-mistakes filter is refitted on every
   closed trade, including the newest ones.

Everything here is pure (no I/O, no data access) so it is unit-tested; the
heavy backtesting lives in ``evolve.py``.
"""

from __future__ import annotations

import random
from typing import Any, Dict, Iterable, List, Optional, Sequence

PROMOTION_MARGIN = 0.05     # rank-score points a challenger must win by
RECENT_TOLERANCE = 0.02     # how much worse on recent data a challenger may be
MAX_LOG_ENTRIES = 104       # two years of weekly generations

GRID_KEYS = ("preset", "signal_threshold", "stop_loss_atr", "trailing_stop_atr", "target_vol_pct", "exit_mode")


def grid_params(full: Dict[str, Any]) -> Dict[str, Any]:
    """The tunable part of a stored profile, in the training grid's terms."""
    out = {k: full[k] for k in GRID_KEYS if k in full and k != "preset"}
    out["preset"] = full.get("preset") or full.get("factor_preset")
    return out


def _key(params: Dict[str, Any]) -> str:
    return "|".join(f"{k}={params.get(k)}" for k in GRID_KEYS)


def neighbours(champion: Dict[str, Any], grid: Dict[str, Sequence[Any]]) -> List[Dict[str, Any]]:
    """Every profile one grid step away from the champion (any one setting changed)."""
    out: List[Dict[str, Any]] = []
    for key in GRID_KEYS:
        values = list(grid[key])
        current = champion.get(key)
        if current not in values:
            continue
        i = values.index(current)
        if key in ("preset", "exit_mode"):
            options = [v for v in values if v != current]          # categorical: try them all
        else:
            options = [values[j] for j in (i - 1, i + 1) if 0 <= j < len(values)]  # ordered: step either way
        for value in options:
            out.append({**champion, key: value})
    return out


def challengers(champion: Dict[str, Any], grid: Dict[str, Sequence[Any]], n_random: int = 6,
                seed: Optional[int] = None) -> List[Dict[str, Any]]:
    """Neighbours plus a few random grid points, unique and never the champion itself."""
    rng = random.Random(seed)
    seen = {_key(champion)}
    unique: List[Dict[str, Any]] = []

    def add(candidate: Dict[str, Any]) -> bool:
        k = _key(candidate)
        if k in seen:
            return False
        seen.add(k)
        unique.append(candidate)
        return True

    for c in neighbours(champion, grid):
        add(c)
    added = 0
    for _ in range(n_random * 20):  # bounded: a tiny grid may not have n_random new points
        if added >= n_random:
            break
        added += add({k: rng.choice(list(grid[k])) for k in GRID_KEYS})
    return unique


def decide(champion: Dict[str, Any], contenders: Iterable[Dict[str, Any]],
           margin: float = PROMOTION_MARGIN, tolerance: float = RECENT_TOLERANCE) -> Dict[str, Any]:
    """Pick this generation's profile.

    Each entry is ``{"params", "rank_score", "holdout_score", "recent_score"}``.
    The best challenger by rank score is promoted only if it clears the margin
    and is not worse (beyond the tolerance) on the holdout and recent windows.
    """
    ranked = sorted(contenders, key=lambda c: c["rank_score"], reverse=True)
    if not ranked:
        return {"promoted": False, "winner": champion, "best_challenger": None,
                "reason": "no challengers could be evaluated; the champion stays"}
    best = ranked[0]
    gain = best["rank_score"] - champion["rank_score"]
    if gain < margin:
        reason = (f"best challenger scored {best['rank_score']:.3f} vs the champion's {champion['rank_score']:.3f} "
                  f"— not the required +{margin:.2f} better, so the champion stays")
        return {"promoted": False, "winner": champion, "best_challenger": best, "reason": reason}
    for window in ("holdout_score", "recent_score"):
        if best.get(window) is not None and champion.get(window) is not None and best[window] < champion[window] - tolerance:
            label = "2023-onward" if window == "holdout_score" else "last-12-months"
            reason = (f"best challenger won on the training windows (+{gain:.3f}) but was worse on the {label} "
                      f"data ({best[window]:.3f} vs {champion[window]:.3f}) — rejected as a likely overfit")
            return {"promoted": False, "winner": champion, "best_challenger": best, "reason": reason}
    reason = (f"challenger beat the champion by +{gain:.3f} on the training windows and held up on recent data "
              f"— promoted")
    return {"promoted": True, "winner": best, "best_challenger": best, "reason": reason}


def append_log(log: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> Dict[str, Any]:
    """Add one generation to the evolution log, numbering it and keeping it bounded."""
    log = dict(log or {})
    generations: List[Dict[str, Any]] = list(log.get("generations") or [])
    entry = {**entry, "generation": (generations[-1]["generation"] + 1) if generations else 1}
    generations.append(entry)
    log["generations"] = generations[-MAX_LOG_ENTRIES:]
    log["promotions"] = sum(1 for g in log["generations"] for k in ("equity", "crypto")
                            if (g.get(k) or {}).get("promoted"))
    return log


def summary(log: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact view for the API and Talk to Loki."""
    generations = list((log or {}).get("generations") or [])
    if not generations:
        return {"generations": 0, "latest": None, "promotions": 0, "history": []}
    latest = generations[-1]
    history = []
    for g in generations[-12:]:
        row = {"generation": g["generation"], "date": g.get("date"), "data_through": g.get("data_through")}
        for kind in ("equity", "crypto"):
            k = g.get(kind) or {}
            row[kind] = {"promoted": bool(k.get("promoted")), "rank_score": (k.get("winner") or {}).get("rank_score"),
                         "challengers": k.get("challengers_tested"), "lessons": (k.get("mistakes") or {}).get("lessons")}
        history.append(row)
    return {"generations": len(generations), "latest": latest, "promotions": (log or {}).get("promotions", 0),
            "history": history}

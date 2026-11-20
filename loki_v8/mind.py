"""Loki's memory: remember every decision, grade it against what happened, learn from mistakes.

The Portfolio AI decides every day, for each of its markets, whether to hold it. This module keeps a
memory of those decisions and studies it:

1. **Remember.** One entry per market per study date: held or not, the trend score and regime the
   AI saw, and the price. Built only from what was known on that date (the replay is causal).
2. **Grade.** ``HORIZON`` trading days later each decision is marked against what the price did:
   a held market that rose was right, a held market that fell was a mistake, a skipped market that
   jumped was a missed move.
3. **Learn lessons.** Decisions are grouped by the conditions the AI saw (regime, trend-score band,
   kind of market, and regime x kind). A group whose held decisions lose money and are right
   clearly less often than the AI's average is a *weak spot*; one that clearly beats it is a
   *strong spot*. Groups need ``MIN_GROUP`` graded decisions before they count.
4. **Prove before acting.** Lessons change nothing until they are proven: weak spots are learned
   from the earlier decisions only, then judged on the later ones they never saw. They are adopted
   only if holding less of them would have made more money on those later decisions AND the
   improvement beats what randomly relabelled groups achieve (a shuffle control). Adopted lessons
   halve the weight of a weak-spot setup in the live plan; otherwise they are shown, not used.

Pure functions only (no I/O, no data fetching), so every step is unit-tested.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

HORIZON = 5  # trading days until a decision is graded
STRIDE = 5  # one study date every STRIDE trading days (keeps graded windows from overlapping)
LOOKBACK_DAYS = 520  # how far back the memory reaches (about two years of trading days)
MIN_GROUP = 25  # graded held decisions a group needs before it can become a lesson
EDGE_PP = 8.0  # how far (percentage points of "right") a group must sit from the average
MISSED_MOVE = {"stock": 0.03, "crypto": 0.06, "haven": 0.02}  # a skipped rise this big is a missed move
LEARN_SHARE = 0.6  # proving lessons: learn on the first 60% of graded dates, judge on the rest
CAUTION = 0.5  # adopted weak spots hold half their usual weight
SHUFFLES = 60
HAVENS = ("GLD", "TLT", "IEF", "SHY", "UUP")


def kind(symbol: str) -> str:
    if symbol.endswith("-USD"):
        return "crypto"
    return "haven" if symbol in HAVENS else "stock"


def score_band(score: Optional[float]) -> str:
    if score is None or not math.isfinite(score):
        return "score unknown"
    if score < 0:
        return "score below 0"
    if score < 30:
        return "score 0-30"
    if score < 60:
        return "score 30-60"
    return "score 60+"


def groups_of(d: Dict[str, Any]) -> List[str]:
    """The condition groups one decision belongs to (its lesson keys)."""
    regime = str(d.get("regime") or "UNKNOWN").replace("_", " ").lower()
    k = kind(d["s"])
    return [f"regime: {regime}", f"{score_band(d.get('score'))}", f"market: {k}", f"{k} in {regime} regime"]


# --- 1. remember -------------------------------------------------------------------------------
def decisions_from_sleeves(sleeves: Dict[str, Dict[str, Any]], lookback: int = LOOKBACK_DAYS,
                           stride: int = STRIDE) -> List[Dict[str, Any]]:
    """Every market's decision on every study date of the last ``lookback`` trading days.

    A sleeve (``portfolio_ai.run_sleeve``) carries daily ``exposure``, ``close``, ``score`` and
    ``regime`` series, each built only from bars up to that day.
    """
    out: List[Dict[str, Any]] = []
    for symbol, sl in sleeves.items():
        close = sl.get("close")
        expo = sl.get("exposure")
        if close is None or expo is None or len(close) < 2:
            continue
        idx = close.index[-lookback:]
        score = sl.get("score")
        regime = sl.get("regime")
        for i in range(0, len(idx), stride):
            day = idx[i]
            px = float(close.loc[day])
            if not math.isfinite(px) or px <= 0:
                continue
            sc = float(score.get(day, float("nan"))) if score is not None else float("nan")
            out.append({
                "d": str(pd.Timestamp(day).date()), "s": symbol,
                "held": bool(float(expo.get(day, 0.0)) > 0.0),
                "w": round(float(expo.get(day, 0.0)), 4),
                "score": round(sc, 1) if math.isfinite(sc) else None,
                "regime": (str(regime.get(day)) if regime is not None and day in regime.index else None),
                "px": round(px, 6),
            })
    out.sort(key=lambda d: (d["d"], d["s"]))
    return out


# --- 2. grade ----------------------------------------------------------------------------------
def grade(decisions: List[Dict[str, Any]], closes: Dict[str, pd.Series], horizon: int = HORIZON) -> List[Dict[str, Any]]:
    """Add the forward return and a verdict to every decision whose ``horizon`` has passed."""
    graded: List[Dict[str, Any]] = []
    positions: Dict[str, Dict[str, int]] = {}
    for s, c in closes.items():
        positions[s] = {str(pd.Timestamp(t).date()): i for i, t in enumerate(c.index)}
    for d in decisions:
        c = closes.get(d["s"])
        pos = positions.get(d["s"], {}).get(d["d"])
        if c is None or pos is None or pos + horizon >= len(c):
            graded.append({**d, "fwd": None, "verdict": "pending"})
            continue
        fwd = float(c.iloc[pos + horizon]) / d["px"] - 1.0
        if d["held"]:
            verdict = "right" if fwd > 0 else "mistake"
        else:
            verdict = "missed" if fwd >= MISSED_MOVE[kind(d["s"])] else "right"
        graded.append({**d, "fwd": round(fwd, 5), "verdict": verdict})
    return graded


# --- 3. learn lessons --------------------------------------------------------------------------
def _held(graded: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for d in graded if d["held"] and d.get("fwd") is not None]


def _stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "right_pct": None, "mean_fwd_pct": None}
    fwd = np.array([r["fwd"] for r in rows], dtype=float)
    return {"n": len(rows), "right_pct": round(100.0 * float((fwd > 0).mean()), 1),
            "mean_fwd_pct": round(100.0 * float(fwd.mean()), 3)}


def lessons(graded: List[Dict[str, Any]], min_group: int = MIN_GROUP, edge_pp: float = EDGE_PP) -> Dict[str, Any]:
    """Weak and strong spots among the held decisions, plus the most-missed moves."""
    held = _held(graded)
    overall = _stats(held)
    by: Dict[str, List[Dict[str, Any]]] = {}
    for d in held:
        for g in groups_of(d):
            by.setdefault(g, []).append(d)
    weak, strong = [], []
    if overall["n"]:
        for g, rows in by.items():
            st = _stats(rows)
            if st["n"] < min_group:
                continue
            gap = st["right_pct"] - overall["right_pct"]
            entry = {"group": g, **st, "vs_average_pp": round(gap, 1)}
            if gap <= -edge_pp and st["mean_fwd_pct"] < 0:
                weak.append({**entry, "lesson": f"Holding when {g}: right {st['right_pct']:.0f}% of {st['n']} times "
                                                 f"(average {overall['right_pct']:.0f}%), average week {st['mean_fwd_pct']:+.2f}%."})
            elif gap >= edge_pp and st["mean_fwd_pct"] > overall["mean_fwd_pct"]:
                strong.append({**entry, "lesson": f"Holding when {g}: right {st['right_pct']:.0f}% of {st['n']} times "
                                                   f"(average {overall['right_pct']:.0f}%), average week {st['mean_fwd_pct']:+.2f}%."})
    weak.sort(key=lambda e: e["vs_average_pp"])
    strong.sort(key=lambda e: -e["vs_average_pp"])
    missed: Dict[str, int] = {}
    for d in graded:
        if d.get("verdict") == "missed":
            missed[d["s"]] = missed.get(d["s"], 0) + 1
    return {"overall": overall, "weak_spots": weak, "strong_spots": strong,
            "most_missed": [{"symbol": s, "times": n} for s, n in sorted(missed.items(), key=lambda kv: -kv[1])[:5]]}


# --- 4. prove before acting ----------------------------------------------------------------------
def _gain(rows: Sequence[Dict[str, Any]], flagged: set) -> float:
    """Extra return from holding CAUTION of a weak-spot setup instead of all of it (per decision, summed)."""
    total = 0.0
    for r in rows:
        if any(g in flagged for g in groups_of(r)):
            total -= (1.0 - CAUTION) * r["w"] * r["fwd"]
    return total


def prove(graded: List[Dict[str, Any]], seed: int = 7) -> Dict[str, Any]:
    """Learn weak spots on earlier decisions, judge them on later ones, against shuffled controls."""
    held = _held(graded)
    dates = sorted({d["d"] for d in held})
    if len(dates) < 20 or len(held) < 4 * MIN_GROUP:
        return {"status": "learning", "why": f"Needs more graded decisions ({len(held)} so far).", "adopted": []}
    cut = dates[int(len(dates) * LEARN_SHARE)]
    early = [d for d in graded if d["d"] < cut]
    late = [d for d in held if d["d"] >= cut]
    flagged = {w["group"] for w in lessons(early, min_group=max(10, MIN_GROUP // 2))["weak_spots"]}
    if not flagged:
        return {"status": "no weak spots", "why": "The earlier decisions showed no clear weak spot to learn from.",
                "adopted": [], "judged_on_from": cut}
    real = _gain(late, flagged)
    flagged_late = [r for r in late if any(g in flagged for g in groups_of(r))]
    rng = random.Random(seed)
    all_groups = sorted({g for d in held for g in groups_of(d)})
    beats = 0
    for _ in range(SHUFFLES):
        fake = set(rng.sample(all_groups, k=min(len(flagged), len(all_groups))))
        if real > _gain(late, fake):
            beats += 1
    evidence = {"learned_before": cut, "flagged_groups": sorted(flagged), "judged_decisions": len(late),
                "flagged_judged": len(flagged_late), "flagged_judged_stats": _stats(flagged_late),
                "gain_pct_points": round(100 * real, 3), "beats_shuffles_pct": round(100 * beats / SHUFFLES, 1)}
    proven = real > 0 and len(flagged_late) >= 15 and beats >= 0.9 * SHUFFLES
    if not proven:
        return {"status": "not proven", "adopted": [], "evidence": evidence,
                "why": "Holding less of the weak spots would not have clearly helped on decisions they were not learned from."}
    adopted = [w["group"] for w in lessons(graded)["weak_spots"]]  # relearned on everything, for live use
    return {"status": "proven", "adopted": adopted, "evidence": evidence,
            "why": "On later decisions it never saw, holding half of the weak spots made more money than "
                   "holding them in full, and more than random groups did."}


def caution_for(position: Dict[str, Any], adopted: Sequence[str]) -> Optional[str]:
    """The adopted lesson that applies to a live position, if any."""
    d = {"s": position.get("symbol", ""), "regime": position.get("regime"), "score": position.get("score")}
    for g in groups_of(d):
        if g in adopted:
            return g
    return None


def recent_form(graded: List[Dict[str, Any]], last: int = 60) -> Dict[str, Any]:
    """Is the AI getting better or worse? Right-rate of its latest held decisions vs all of them."""
    held = _held(graded)
    if len(held) < last + 20:
        return {"status": "not enough yet"}
    recent, before = _stats(held[-last:]), _stats(held[:-last])
    gap = recent["right_pct"] - before["right_pct"]
    trend = "improving" if gap >= 5 else "slipping" if gap <= -5 else "steady"
    return {"status": trend, "recent": recent, "before": before, "gap_pp": round(gap, 1)}


def study(sleeves: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """One full study session: remember, grade, learn, prove."""
    closes = {s: v["close"] for s, v in sleeves.items() if v.get("close") is not None}
    graded = grade(decisions_from_sleeves(sleeves), closes)
    done = [d for d in graded if d["verdict"] != "pending"]
    verdicts: Dict[str, int] = {}
    for d in done:
        verdicts[d["verdict"]] = verdicts.get(d["verdict"], 0) + 1
    les = lessons(graded)
    proof = prove(graded)
    return {
        "remembered": len(graded), "graded": len(done), "pending": len(graded) - len(done),
        "from": graded[0]["d"] if graded else None, "to": graded[-1]["d"] if graded else None,
        "verdicts": verdicts, "lessons": les, "proof": proof, "form": recent_form(graded),
        "recent_mistakes": [{k: d[k] for k in ("d", "s", "score", "regime", "fwd")}
                            for d in sorted((d for d in done if d["verdict"] == "mistake"), key=lambda d: d["d"])[-8:]],
        "how": (f"Every {STRIDE} trading days over the last {LOOKBACK_DAYS}, each market's decision is remembered and "
                f"graded {HORIZON} trading days later. Lessons need {MIN_GROUP}+ decisions and change the plan only "
                "once proven on decisions they were not learned from."),
    }

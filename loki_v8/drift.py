"""LOKI v8 — drift alarm: is the live (paper) Day Trader behaving like its test?

Pure statistics, no network, so every number can be checked by hand. Three signals,
each compared with the tested record of the same markets on the years the rules
never saw:

* **Win rate.** The share of round trips (one market, one day) that made money, as a
  z-score against the tested rate: ``z = (p_live - p0) / sqrt(p0 (1 - p0) / N)``.
* **Payoff.** The average winning round trip divided by the average losing one, as a
  fraction of the tested payoff. It catches winners being cut short.
* **Slippage.** How far each fill landed from the price the rules decided on, in basis
  points against the trade (positive = worse), compared with the cost the test assumed.

Tiers, judged only once there are ``MIN_TRADES`` round trips:

* green  — size x1.0: everything within range;
* amber  — size x0.5: z below -1.5, payoff under 60% of the test's, or slippage over twice
  the assumed cost;
* red    — size x0.1: z below -2.5, or payoff under 35% of the test's.

The multiplier only ever shrinks new positions. It never opens one, and never blocks a close.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

MIN_TRADES = 20  # round trips needed before the alarm may act
WINDOW = 30  # the most recent round trips judged
Z_AMBER, Z_RED = -1.5, -2.5
PAYOFF_AMBER, PAYOFF_RED = 0.60, 0.35  # live payoff as a share of the tested payoff
SLIPPAGE_AMBER = 2.0  # live slippage as a multiple of the cost the test assumed
MULTIPLIER = {"green": 1.0, "amber": 0.5, "red": 0.1}


def round_trips(fills: Iterable[Dict[str, Any]], symbols: Iterable[str]) -> List[Dict[str, Any]]:
    """Completed round trips (one market, one New York day, ending flat) from broker fills, oldest first.

    Each fill: ``symbol``, ``side`` ("buy", "sell" or "sell_short"), ``qty``, ``price`` and ``day``.
    A day whose shares do not net to zero is still open (or was traded by hand) and is left out.
    """
    wanted = set(symbols)
    books: Dict[tuple, Dict[str, float]] = defaultdict(lambda: {"net": 0.0, "cash": 0.0, "bought": 0.0})
    for fill in fills:
        symbol = str(fill.get("symbol"))
        if symbol not in wanted:
            continue
        try:
            qty, price = float(fill["qty"]), float(fill["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(qty) and math.isfinite(price)) or qty <= 0 or price <= 0:
            continue
        sign = 1.0 if str(fill.get("side")) == "buy" else -1.0
        book = books[(str(fill.get("day")), symbol)]
        book["net"] += sign * qty
        book["cash"] -= sign * qty * price
        book["bought"] += qty * price
    trips = []
    for (day, symbol), book in sorted(books.items()):
        if abs(book["net"]) < 1e-9 and book["bought"] > 0:
            trips.append({"day": day, "symbol": symbol, "pnl": round(book["cash"], 2)})
    return trips


def baseline(market_baselines: Dict[str, Dict[str, Any]], symbols: Iterable[str]) -> Dict[str, Optional[float]]:
    """The tested win rate (0-1) and payoff of the given markets, averaged with equal weight."""
    rows = [market_baselines.get(s) or {} for s in symbols]
    wins = [r["win_days_pct"] / 100 for r in rows if r.get("win_days_pct")]
    payoffs = [r["payoff"] for r in rows if r.get("payoff")]
    return {"win_rate": mean(wins) if wins else None, "payoff": mean(payoffs) if payoffs else None}


def assess(trips: List[Dict[str, Any]], market_baselines: Dict[str, Dict[str, Any]],
           slippage_bps: List[float], assumed_cost_bps: float) -> Dict[str, Any]:
    """The drift tier, its size multiplier and every number behind it."""
    recent = trips[-WINDOW:]
    n = len(recent)
    base = baseline(market_baselines, {t["symbol"] for t in recent})
    wins = [t["pnl"] for t in recent if t["pnl"] > 0]
    losses = [-t["pnl"] for t in recent if t["pnl"] < 0]
    win_rate = len(wins) / n if n else None
    payoff = (mean(wins) / mean(losses)) if wins and losses else None
    slip = mean(slippage_bps[-WINDOW:]) if slippage_bps else None
    out: Dict[str, Any] = {
        "trades": n, "min_trades": MIN_TRADES, "win_rate": win_rate, "expected_win_rate": base["win_rate"],
        "payoff": payoff, "expected_payoff": base["payoff"], "slippage_bps": slip, "assumed_cost_bps": assumed_cost_bps,
        "z": None, "payoff_vs_test": None, "slippage_vs_assumed": None, "reasons": [],
    }
    if win_rate is not None and base["win_rate"] and 0 < base["win_rate"] < 1:
        out["z"] = (win_rate - base["win_rate"]) / math.sqrt(base["win_rate"] * (1 - base["win_rate"]) / n)
    if payoff is not None and base["payoff"]:
        out["payoff_vs_test"] = payoff / base["payoff"]
    if slip is not None and assumed_cost_bps > 0:
        out["slippage_vs_assumed"] = slip / assumed_cost_bps
    tier = "green"
    if n < MIN_TRADES:
        out["reasons"].append(f"warming up: {n} of {MIN_TRADES} round trips so far; size stays x1.0")
    else:
        z, ratio, slipx = out["z"], out["payoff_vs_test"], out["slippage_vs_assumed"]
        if (z is not None and z < Z_RED) or (ratio is not None and ratio < PAYOFF_RED):
            tier = "red"
        elif (z is not None and z < Z_AMBER) or (ratio is not None and ratio < PAYOFF_AMBER) or (slipx is not None and slipx > SLIPPAGE_AMBER):
            tier = "amber"
        if z is not None and z < Z_AMBER:
            out["reasons"].append(f"win rate {win_rate:.0%} vs {base['win_rate']:.0%} tested (z = {z:.2f})")
        if ratio is not None and ratio < PAYOFF_AMBER:
            out["reasons"].append(f"payoff {payoff:.2f} vs {base['payoff']:.2f} tested")
        if slipx is not None and slipx > SLIPPAGE_AMBER:
            out["reasons"].append(f"slippage {slip:.1f} bp vs {assumed_cost_bps:.1f} bp assumed")
    out.update(tier=tier, multiplier=MULTIPLIER[tier])
    for key in ("win_rate", "expected_win_rate", "payoff", "expected_payoff", "slippage_bps", "z", "payoff_vs_test", "slippage_vs_assumed"):
        if isinstance(out[key], float):
            out[key] = round(out[key], 3)
    return out

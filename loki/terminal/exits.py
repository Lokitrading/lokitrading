"""Exit management for seconds-to-minutes memecoin scalps (no long holds).

Every open position carries an ``ExitPlan``; ``step`` is called on each new price and returns the
sells to make, as fractions of the ORIGINAL position:

* hard stop: price at or below entry x (1 + ``stop_loss``) sells everything;
* take-profit tiers: e.g. (2.0, 0.5) sells 50% at 2x (recovering the AUD put in), later tiers
  sell more; whatever is left rides as the moonbag;
* trailing stop: once price is ``trail_arm`` above entry, a fall of ``trail`` from the highest
  price since entry sells the rest (the moonbag too);
* stale exit: if price has not moved ``stale_move`` either way within ``stale_after_s``, sell all;
* max hold: nothing is held longer than ``max_hold_s``.

Pure and deterministic; the paper engine and the live engine use the same plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


@dataclass
class ExitPlan:
    stop_loss: float = -0.25  # -25% from entry
    tiers: Sequence[Tuple[float, float]] = ((2.0, 0.5),)  # (multiple of entry, share of original to sell)
    trail: Optional[float] = 0.25  # sell the rest after a 25% fall from the high...
    trail_arm: float = 0.30  # ...once price has been 30% above entry
    stale_after_s: Optional[float] = 180.0
    stale_move: float = 0.05
    max_hold_s: float = 1800.0


@dataclass
class Position:
    entry_price: float
    entry_time: float
    plan: ExitPlan = field(default_factory=ExitPlan)
    remaining: float = 1.0  # share of the original still held
    high: float = 0.0
    tiers_done: int = 0
    moved: bool = False
    closed_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError("entry price must be positive")
        self.high = max(self.high, self.entry_price)


def step(pos: Position, price: float, now: float) -> List[Tuple[float, str]]:
    """Sells due at this price and time, as (share of the original position, reason)."""
    if pos.remaining <= 1e-12 or pos.closed_reason:
        return []
    p, out = pos.plan, []
    ret = price / pos.entry_price - 1.0
    pos.high = max(pos.high, price)
    if abs(ret) >= p.stale_move:
        pos.moved = True

    def sell_all(reason: str) -> List[Tuple[float, str]]:
        out.append((pos.remaining, reason))
        pos.remaining = 0.0
        pos.closed_reason = reason
        return out

    if ret <= p.stop_loss:
        return sell_all("stop loss")
    while pos.tiers_done < len(p.tiers) and price >= pos.entry_price * p.tiers[pos.tiers_done][0]:
        mult, share = p.tiers[pos.tiers_done]
        share = min(share, pos.remaining)
        pos.tiers_done += 1
        if share > 0:
            pos.remaining -= share
            out.append((share, f"take profit {mult:g}x"))
        if pos.remaining <= 1e-12:
            pos.closed_reason = f"take profit {mult:g}x"
            return out
    armed = pos.high >= pos.entry_price * (1 + p.trail_arm)
    if p.trail is not None and armed and price <= pos.high * (1 - p.trail):
        return sell_all("trailing stop")
    held = now - pos.entry_time
    if p.stale_after_s is not None and held >= p.stale_after_s and not pos.moved:
        return sell_all("stale: no move")
    if held >= p.max_hold_s:
        return sell_all("max hold time")
    return out

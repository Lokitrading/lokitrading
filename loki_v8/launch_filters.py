"""New-launch memecoin screening and exits, for shadow (paper) trading only.

Pure functions: no network. The runner (``memecoin_shadow.py``) and the site's launch radar feed
them RugCheck reports, DexScreener pair data, Solana RPC transactions and Jupiter quotes.

Safety filters (any one failing rejects the token before anything else looks at it):

* mint authority revoked, freeze authority revoked;
* liquidity locked or burned (at least ``MIN_LP_LOCKED_PCT``), or a launchpad bonding curve;
* top 10 holders, leaving out pools, lockers and the bonding curve, at most ``MAX_TOP10_PCT``;
* no supply bundled at launch: wallets other than pools buying ``MAX_LAUNCH_BUNDLE_PCT`` or more
  of the supply in the launch slot and the next one;
* no danger-level RugCheck risk (for example a creator with a history of rugged tokens), not
  already marked rugged, no insider network detected.

Entry (only after the safety filters pass): organic velocity. Five-minute volume at least
``MIN_VOL_TO_LIQ`` times the pool's liquidity, buys at least ``MIN_BUY_SELL`` times sells, and
liquidity of at least ``MIN_LIQUIDITY_USD`` so a small order is not the whole market.
(DexScreener counts transactions, not unique wallets, so wash trading by one wallet can still pass.)

Exits (``ExitPlan``): sell 50% at 2x, 25% at 4.5x, and trail the last 25% with a stop 30% under
its peak; leave entirely if the trade has not reached +20% within 12 minutes, if it falls 30%
below entry (a hard stop the original plan did not have; rugs fall faster than a time stop), or if
the creator or a tracked top-5 holder sells more than half their tokens.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

MIN_LP_LOCKED_PCT = 90.0
MAX_TOP10_PCT = 25.0
MAX_LAUNCH_BUNDLE_PCT = 40.0
MIN_VOL_TO_LIQ = 2.0
MIN_BUY_SELL = 1.5
MIN_LIQUIDITY_USD = 5_000.0
POOL_TYPES = {"AMM", "LOCKER"}  # RugCheck's labels for pools, bonding curves and lockers


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def holders_outside_pools(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Top holders that are people or bots, not pools, bonding curves or lockers."""
    known = report.get("knownAccounts") or {}
    pools = {m.get("pubkey") for m in report.get("markets") or [] if isinstance(m, dict)}
    out = []
    for h in report.get("topHolders") or []:
        owner = h.get("owner")
        if owner in pools or (known.get(owner) or {}).get("type") in POOL_TYPES:
            continue
        out.append(h)
    return out


def safety(report: Dict[str, Any], launch_bundle_pct: Optional[float] = None) -> List[str]:
    """Reasons to reject the token on safety grounds (empty when every safety filter passes)."""
    reasons: List[str] = []
    if not report or not report.get("mint"):
        return ["no safety report"]
    if report.get("mintAuthority"):
        reasons.append("mint authority not revoked: more tokens can be printed")
    if report.get("freezeAuthority"):
        reasons.append("freeze authority not revoked: holders can be frozen")
    if report.get("rugged"):
        reasons.append("already marked rugged")
    on_curve = any((m.get("marketType") or "").startswith("pump") for m in report.get("markets") or [] if isinstance(m, dict))
    locked = [_num((m.get("lp") or {}).get("lpLockedPct")) for m in report.get("markets") or [] if isinstance(m, dict)]
    best_lock = max([x for x in locked if x is not None], default=None)
    if not on_curve and (best_lock is None or best_lock < MIN_LP_LOCKED_PCT):
        reasons.append(f"liquidity not locked or burned ({0 if best_lock is None else round(best_lock)}% locked)")
    top10 = sum(_num(h.get("pct")) or 0.0 for h in holders_outside_pools(report)[:10])
    if top10 > MAX_TOP10_PCT:
        reasons.append(f"top 10 holders own {top10:.0f}% (limit {MAX_TOP10_PCT:.0f}%)")
    if (report.get("graphInsidersDetected") or 0) > 0:
        reasons.append(f"{report.get('graphInsidersDetected')} insider wallets linked to each other")
    for risk in report.get("risks") or []:
        if str(risk.get("level") or "").lower() == "danger":
            reasons.append(f"RugCheck: {risk.get('name')}")
    if launch_bundle_pct is not None and launch_bundle_pct >= MAX_LAUNCH_BUNDLE_PCT:
        reasons.append(f"{launch_bundle_pct:.0f}% of supply bought in the launch block and the next (bundled)")
    return reasons


def velocity(pair: Optional[Dict[str, Any]], liquidity_usd: Optional[float]) -> Tuple[List[str], Dict[str, Any]]:
    """Reasons not to enter on momentum (empty when it qualifies), and the figures used."""
    pair = pair or {}
    volume = _num((pair.get("volume") or {}).get("m5")) or 0.0
    txns = (pair.get("txns") or {}).get("m5") or {}
    buys, sells = int(txns.get("buys") or 0), int(txns.get("sells") or 0)
    liq = _num((pair.get("liquidity") or {}).get("usd")) or liquidity_usd or 0.0
    figures = {"volume_m5_usd": round(volume, 2), "liquidity_usd": round(liq, 2), "buys_m5": buys, "sells_m5": sells,
               "vol_to_liq": round(volume / liq, 2) if liq else None, "buy_sell": round(buys / sells, 2) if sells else (float(buys) if buys else 0.0)}
    reasons = []
    if liq < MIN_LIQUIDITY_USD:
        reasons.append(f"liquidity ${liq:,.0f} under ${MIN_LIQUIDITY_USD:,.0f}")
    if not liq or volume / liq < MIN_VOL_TO_LIQ:
        reasons.append(f"5-min volume {figures['vol_to_liq'] or 0}x liquidity (needs {MIN_VOL_TO_LIQ}x)")
    if sells and buys / sells < MIN_BUY_SELL or not buys:
        reasons.append(f"buys/sells {figures['buy_sell']} (needs {MIN_BUY_SELL})")
    return reasons, figures


def launch_bundle_pct(transactions: Sequence[Dict[str, Any]], mint: str, supply: float, pools: Sequence[str]) -> Optional[float]:
    """Share of supply that wallets other than pools received in the first two slots of trading.

    ``transactions`` are Solana ``getTransaction`` results (jsonParsed) for the token's earliest
    signatures. Returns None when there is not enough to judge.
    """
    if not transactions or not supply:
        return None
    slots = sorted({int(t["slot"]) for t in transactions if t.get("slot") is not None})
    early = set(slots[:2])
    pool_set = set(pools)
    gained = 0.0
    for t in transactions:
        if t.get("slot") not in early:
            continue
        meta = t.get("meta") or {}
        if meta.get("err"):
            continue
        before = {(b.get("owner"), b.get("mint")): _num((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0 for b in meta.get("preTokenBalances") or []}
        for b in meta.get("postTokenBalances") or []:
            if b.get("mint") != mint or b.get("owner") in pool_set:
                continue
            after = _num((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
            gained += max(0.0, after - before.get((b.get("owner"), mint), 0.0))
    return round(gained / supply * 100, 2)


@dataclass
class ExitPlan:
    """Laddered take-profits, a trailing moonbag, a time stop, a hard stop and holder-sell triggers."""

    take_profits: Tuple[Tuple[float, float], ...] = ((2.0, 0.50), (4.5, 0.25))  # (multiple of entry, share of the original position)
    trail_from_peak: float = 0.30
    time_stop_minutes: float = 12.0
    time_stop_gain: float = 0.20
    hard_stop: float = 0.30
    holder_sell_trigger: float = 0.50


@dataclass
class Position:
    mint: str
    entry_price: float
    opened_at: float  # unix seconds
    remaining: float = 1.0  # share of the original position still held
    peak: float = 0.0
    taken: List[float] = field(default_factory=list)  # take-profit levels already sold
    holders: Dict[str, float] = field(default_factory=dict)  # tracked holder accounts -> balance at entry


def step(pos: Position, price: float, now: float, plan: ExitPlan,
         holder_balances: Optional[Dict[str, float]] = None) -> List[Tuple[float, str]]:
    """Sales to make at this price: a list of (share of the original position, reason). Mutates ``pos``."""
    if pos.remaining <= 1e-9 or price <= 0:
        return []
    multiple = price / pos.entry_price
    pos.peak = max(pos.peak, multiple)
    sells: List[Tuple[float, str]] = []

    def sell_all(reason: str) -> List[Tuple[float, str]]:
        share, pos.remaining = pos.remaining, 0.0
        return sells + [(share, reason)]

    if holder_balances is not None:
        for account, start in pos.holders.items():
            now_balance = holder_balances.get(account)
            if start > 0 and now_balance is not None and now_balance < start * (1 - plan.holder_sell_trigger):
                return sell_all(f"tracked holder {account[:6]} sold over {plan.holder_sell_trigger:.0%}")
    if multiple <= 1 - plan.hard_stop:
        return sell_all(f"hard stop: {multiple - 1:+.0%}")
    if not pos.taken and (now - pos.opened_at) / 60 >= plan.time_stop_minutes and pos.peak < 1 + plan.time_stop_gain:
        return sell_all(f"time stop: under +{plan.time_stop_gain:.0%} after {plan.time_stop_minutes:.0f} min")
    for level, share in plan.take_profits:
        if multiple >= level and level not in pos.taken:
            pos.taken.append(level)
            portion = min(share, pos.remaining)
            pos.remaining -= portion
            sells.append((portion, f"take profit at {level}x"))
    if pos.taken and pos.remaining > 1e-9 and multiple <= pos.peak * (1 - plan.trail_from_peak):
        return sell_all(f"trailing stop: {plan.trail_from_peak:.0%} under the {pos.peak:.2f}x peak")
    return sells

"""Anti-rug and honeypot shield: a pre-flight check that must pass before any buy.

Checks, in order (any failure blocks the trade):

1. **Mint authority disabled** - nobody can print more of the token. Read on-chain
   (``getAccountInfo``, jsonParsed) through the RPC pool; RugCheck's report is the fallback.
2. **Freeze authority disabled** - nobody can freeze your tokens so you can never sell (the classic
   honeypot).
3. **Top-10 holder concentration** below ``max_top10_pct``, counting real wallets only: liquidity
   pools, the pump.fun bonding curve and other known program accounts are left out (they are not
   holders who can dump).
4. **Liquidity cannot be pulled**: for pools that issue LP tokens (Raydium AMM/CPMM, PumpSwap,
   Meteora dynamic), the largest pool's LP must be at least ``min_lp_locked_pct`` burned or locked.
   Concentrated-liquidity pools (Orca Whirlpool, Raydium CLMM, Meteora DLMM) have no LP token to
   burn, so they are judged on depth instead; a token still on the pump.fun bonding curve has its
   liquidity held by the curve program, which no one can withdraw.
5. **Not flagged**: RugCheck marks it rugged, or lists a "danger" level risk.

RugCheck (api.rugcheck.xyz) supplies holders, pools and LP locks; the authority checks are
verified on-chain. ``check`` returns every check with a plain reason, and ``ok`` only if all pass.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from . import rpc

CL_POOLS = {"orca", "whirlpool", "raydium_clmm", "meteoradlmm", "meteora_dlmm", "dlmm"}  # no LP token
CURVE_PROGRAMS = {"pump_fun", "pumpfun", "pump.fun"}
CACHE_S = 20.0


@dataclass
class ShieldLimits:
    max_top10_pct: float = 30.0
    min_lp_locked_pct: float = 90.0
    min_liquidity_usd: float = 5_000.0


_cache: Dict[str, Any] = {}


def rugcheck_report(mint: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=8,
                         headers={"Accept": "application/json", "User-Agent": "Loki-terminal"}, allow_redirects=False)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def onchain_authorities(mint: str, call: Optional[Callable[[str, List[Any]], Any]] = None) -> Optional[Dict[str, Any]]:
    """Mint and freeze authority straight from the chain (None when the RPC cannot answer)."""
    call = call or rpc.pool().call
    try:
        res = call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        info = res["value"]["data"]["parsed"]["info"]
        return {"mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
                "supply": info.get("supply"), "decimals": info.get("decimals")}
    except Exception:  # noqa: BLE001 - not a token mint, or RPC down
        return None


def _norm(kind: Any) -> str:
    return str(kind or "").lower().replace(" ", "_")


def evaluate(report: Optional[Dict[str, Any]], chain: Optional[Dict[str, Any]],
             limits: ShieldLimits = ShieldLimits(), stream_top10_pct: Optional[float] = None) -> Dict[str, Any]:
    """Pure decision from RugCheck's report and the on-chain authorities."""
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    rep = report or {}
    src = chain if chain is not None else {"mint_authority": rep.get("mintAuthority"), "freeze_authority": rep.get("freezeAuthority")}
    where = "on-chain" if chain is not None else "RugCheck"
    if chain is None and report is None:
        add("data", False, "Neither the chain nor RugCheck answered, so the token cannot be checked.")
        return {"ok": False, "checks": checks, "blocked_by": ["data"]}
    add("mint authority disabled", src.get("mint_authority") in (None, ""),
        f"no one can mint more ({where})" if not src.get("mint_authority") else f"mint authority is still set ({where}): supply can be inflated")
    add("freeze authority disabled", src.get("freeze_authority") in (None, ""),
        f"no one can freeze holders ({where})" if not src.get("freeze_authority") else f"freeze authority is set ({where}): your tokens could be frozen (honeypot)")

    known = {k: _norm((v or {}).get("type")) for k, v in (rep.get("knownAccounts") or {}).items()}
    holders = rep.get("topHolders") or []
    if holders:
        real = [h for h in holders if known.get(h.get("owner", "")) not in ("amm", "pool", "program", "locker", "bonding_curve")
                and known.get(h.get("address", "")) not in ("amm", "pool", "program", "locker", "bonding_curve")]
        top10 = sum(float(h.get("pct") or 0) for h in real[:10])
        add("top-10 holders below limit", top10 <= limits.max_top10_pct,
            f"top 10 real wallets hold {top10:.1f}% (limit {limits.max_top10_pct:g}%; pools and programs excluded)")
    elif stream_top10_pct is not None and not (rep.get("markets") or []):
        # A launch only seconds old is not indexed yet; on the bonding curve, the live trade stream
        # shows every buy, so its holder shares are complete for this token.
        add("top-10 holders below limit", stream_top10_pct <= limits.max_top10_pct,
            f"top 10 buyers hold {stream_top10_pct:.1f}% (live trade stream; limit {limits.max_top10_pct:g}%)")
    else:
        add("top-10 holders below limit", False, "holder list unavailable, so concentration cannot be checked")

    markets = rep.get("markets") or []
    lp_markets = [m for m in markets if _norm(m.get("marketType")) not in CL_POOLS and _norm(m.get("marketType")) not in CURVE_PROGRAMS]
    liquidity = float(rep.get("totalMarketLiquidity") or 0)
    on_curve = not markets and (stream_top10_pct is not None or _norm(rep.get("launchpad", {}).get("name") if isinstance(rep.get("launchpad"), dict) else rep.get("launchpad")) in CURVE_PROGRAMS)
    if lp_markets:
        biggest = max(lp_markets, key=lambda m: float((m.get("lp") or {}).get("lpLockedUSD") or (m.get("lp") or {}).get("quoteUSD") or 0))
        locked = float((biggest.get("lp") or {}).get("lpLockedPct") or 0)
        add("liquidity locked or burned", locked >= limits.min_lp_locked_pct,
            f"{biggest.get('marketType')} pool LP {locked:.0f}% burned/locked (needs {limits.min_lp_locked_pct:g}%)")
    elif markets:
        add("liquidity locked or burned", liquidity >= limits.min_liquidity_usd,
            f"concentrated-liquidity pools only (no LP token to burn); depth US${liquidity:,.0f} "
            f"(needs US${limits.min_liquidity_usd:,.0f})")
    elif on_curve:
        add("liquidity locked or burned", True, "still on the pump.fun bonding curve: liquidity is held by the curve program")
    else:
        add("liquidity locked or burned", False, "no pool found, so liquidity cannot be checked")

    danger = [r.get("name") for r in rep.get("risks") or [] if str(r.get("level", "")).lower() == "danger"]
    flagged = bool(rep.get("rugged")) or bool(danger)
    add("not flagged", not flagged, "RugCheck marks it rugged" if rep.get("rugged") else
        (f"danger: {', '.join(danger)}" if danger else "no danger-level risks"))
    blocked = [c["check"] for c in checks if not c["ok"]]
    return {"ok": not blocked, "checks": checks, "blocked_by": blocked}


def check(mint: str, limits: ShieldLimits = ShieldLimits(), now: Optional[float] = None,
          stream_top10_pct: Optional[float] = None) -> Dict[str, Any]:
    """Run the shield for one mint (cached briefly so a burst of signals does not re-query)."""
    now = time.time() if now is None else now
    hit = _cache.get(mint) if stream_top10_pct is None else None
    if hit and now - hit[0] < CACHE_S:
        out: Dict[str, Any] = hit[1]
        return out
    chain = onchain_authorities(mint)
    result = evaluate(rugcheck_report(mint), chain, limits, stream_top10_pct)
    if chain is not None:
        result["decimals"] = chain.get("decimals")
    result["mint"] = mint
    _cache[mint] = (now, result)
    return result

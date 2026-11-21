"""Terminal API: live AUD rates, the anti-rug shield, Jupiter quotes in AUD, RPC health. Read-only.

Nothing here signs or sends a transaction: quotes and shield results feed the paper engine and,
in the desktop app's live mode, the user's own signer.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Response

from . import aud, costs, jupiter, rpc, shield

router = APIRouter(prefix="/api/terminal", tags=["terminal"])
MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _rates() -> Dict[str, Any]:
    try:
        return aud.rates()
    except RuntimeError as error:
        raise HTTPException(503, str(error))


@router.get("/rates")
def terminal_rates(response: Response) -> Dict[str, Any]:
    """SOL/AUD, USDC/AUD and SOL/USD, live, with sources and freshness."""
    _no_store(response)
    out = _rates()
    out["round_trip_fees_aud"] = {
        "priority": round(costs.round_trip_fees_aud(out["sol_aud"]), 4),
        "jito_0.0001_sol": round(costs.round_trip_fees_aud(out["sol_aud"], jito_tip_lamports=100_000), 4)}
    return out


@router.get("/shield")
def terminal_shield(response: Response, mint: str = Query(..., min_length=32, max_length=44),
                    max_top10_pct: float = Query(30.0, ge=5, le=100),
                    min_lp_locked_pct: float = Query(90.0, ge=0, le=100)) -> Dict[str, Any]:
    """Anti-rug pre-flight for a token: every check, and whether a buy would be allowed."""
    _no_store(response)
    if not MINT.match(mint):
        raise HTTPException(422, "That is not a Solana token address.")
    return shield.check(mint, shield.ShieldLimits(max_top10_pct=max_top10_pct, min_lp_locked_pct=min_lp_locked_pct))


@router.get("/quote")
def terminal_quote(response: Response, mint: str = Query(..., min_length=32, max_length=44),
                   side: str = Query("buy", pattern="^(buy|sell)$"),
                   aud_amount: float = Query(20.0, gt=0, le=100_000),
                   tokens: int = Query(0, ge=0), decimals: int = Query(6, ge=0, le=12),
                   jito_tip_lamports: int = Query(0, ge=0, le=10_000_000)) -> Dict[str, Any]:
    """A Jupiter quote in AUD (buy: spend ``aud_amount``; sell: ``tokens`` raw units), with the shield for buys."""
    _no_store(response)
    if not MINT.match(mint):
        raise HTTPException(422, "That is not a Solana token address.")
    r = _rates()
    settings = jupiter.SwapSettings(jito_tip_lamports=jito_tip_lamports)
    try:
        if side == "buy":
            q = jupiter.quote(jupiter.SOL_MINT, mint, aud.aud_to_lamports(aud_amount, r["sol_aud"]), settings)
        else:
            if tokens <= 0:
                raise HTTPException(422, "Give the number of tokens to sell (raw units).")
            q = jupiter.quote(mint, jupiter.SOL_MINT, tokens, settings)
    except RuntimeError as error:
        raise HTTPException(502, str(error))
    out = jupiter.summary(q, r["sol_aud"], side, decimals)
    out["fees_aud_estimate"] = round(costs.round_trip_fees_aud(r["sol_aud"], jito_tip_lamports=jito_tip_lamports) / 2, 5)
    out["sol_aud"] = r["sol_aud"]
    if side == "buy":
        out["shield"] = shield.check(mint)
        out["allowed"] = out["shield"]["ok"]
    return out


@router.get("/rpc")
def terminal_rpc(response: Response) -> Dict[str, Any]:
    """Health of the Solana RPC endpoints the terminal fails over between."""
    _no_store(response)
    return {"endpoints": rpc.pool().status()}

"""Jupiter swap routing (quote + unsigned swap transaction), tuned for memecoin volatility.

Jupiter's v6 swap API is served at ``/swap/v1`` (lite-api.jup.ag, or api.jup.ag with a key in
``JUPITER_API_KEY``). Every swap is built with:

* ``dynamicSlippage``: Jupiter simulates the route and sets slippage to what the market needs,
  capped by ``max_slippage_bps``;
* ``dynamicComputeUnitLimit``: the compute budget is set from a simulation (no wasted priority fee);
* priority fees at a chosen level up to ``max_priority_lamports``, or a **Jito tip** of
  ``jito_tip_lamports`` (Jupiter adds the tip transfer; the signed transaction is then sent to Jito's
  block engine so it lands in a bundle and cannot be sandwiched).

This module never signs. The swap transaction comes back base64-encoded and unsigned; only the
desktop app's live mode, with the user's own key on their own computer, signs and sends it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Union

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
JITO_BLOCK_ENGINE = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"


def base_url() -> str:
    return "https://api.jup.ag" if os.environ.get("JUPITER_API_KEY") else "https://lite-api.jup.ag"


def _headers() -> Dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": "Loki-terminal"}
    key = os.environ.get("JUPITER_API_KEY")
    if key:
        h["x-api-key"] = key
    return h


@dataclass
class SwapSettings:
    max_slippage_bps: int = 1500  # 15%: memecoins move fast; dynamic slippage usually uses far less
    priority_level: str = "veryHigh"
    max_priority_lamports: int = 2_000_000  # 0.002 SOL cap on the priority fee
    jito_tip_lamports: int = 0  # > 0: pay a Jito tip instead of a priority fee and send via Jito


def quote(input_mint: str, output_mint: str, amount: int, settings: SwapSettings = SwapSettings(),
          timeout: float = 5.0) -> Dict[str, Any]:
    """Best route for ``amount`` (smallest units of the input token)."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    params: Dict[str, Union[str, int]] = {"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount),
              "slippageBps": settings.max_slippage_bps, "restrictIntermediateTokens": "true"}
    r = requests.get(base_url() + "/swap/v1/quote", params=params, headers=_headers(), timeout=timeout,
                     allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(f"Jupiter quote failed (HTTP {r.status_code}): {r.text[:200]}")
    q: Dict[str, Any] = r.json()
    return q


def swap_body(q: Dict[str, Any], user_pubkey: str, settings: SwapSettings = SwapSettings()) -> Dict[str, Any]:
    """The /swap request: dynamic slippage and compute limit, priority fee or Jito tip."""
    if settings.jito_tip_lamports > 0:
        fee: Dict[str, Any] = {"jitoTipLamports": int(settings.jito_tip_lamports)}
    else:
        fee = {"priorityLevelWithMaxLamports": {"maxLamports": int(settings.max_priority_lamports),
                                                "priorityLevel": settings.priority_level}}
    return {"quoteResponse": q, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "dynamicSlippage": True, "prioritizationFeeLamports": fee}


def build_swap(q: Dict[str, Any], user_pubkey: str, settings: SwapSettings = SwapSettings(),
               timeout: float = 6.0) -> Dict[str, Any]:
    """Unsigned swap transaction (base64) plus the fees Jupiter set for it."""
    r = requests.post(base_url() + "/swap/v1/swap", json=swap_body(q, user_pubkey, settings), headers=_headers(),
                      timeout=timeout, allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(f"Jupiter swap build failed (HTTP {r.status_code}): {r.text[:200]}")
    out: Dict[str, Any] = r.json()
    return out


def summary(q: Dict[str, Any], sol_aud: float, side: str, decimals: int = 6) -> Dict[str, Any]:
    """A quote in plain AUD terms: what goes in, what comes out, price impact, route."""
    in_amt, out_amt = int(q["inAmount"]), int(q["outAmount"])
    if side == "buy":  # SOL -> token
        sol = in_amt / 1e9
        tokens = out_amt / 10 ** decimals
    else:  # token -> SOL
        sol = out_amt / 1e9
        tokens = in_amt / 10 ** decimals
    price_aud = (sol * sol_aud / tokens) if tokens else None
    return {"side": side, "sol": round(sol, 9), "aud": round(sol * sol_aud, 4), "tokens": tokens,
            "price_aud": price_aud, "price_impact_pct": round(100 * float(q.get("priceImpactPct") or 0), 3),
            "route": [step.get("swapInfo", {}).get("label") for step in q.get("routePlan", [])],
            "slippage_bps": q.get("slippageBps")}

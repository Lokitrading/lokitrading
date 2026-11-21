"""True net P&L in AUD: every Solana cost of a round trip, deducted.

A swap transaction pays:
* the base fee: ``BASE_FEE_LAMPORTS`` per signature (one signer here);
* the priority fee: compute-unit price (micro-lamports per CU) x compute units used;
* a Jito tip when sent through Jito (a transfer to a tip account, in lamports);
* slippage: the gap between the price quoted at decision time and the price filled, which is a
  cost in AUD even though no fee line shows it.

Buys and sells each pay their own fees. ``net_pnl`` returns gross P&L, each cost and the realised
net, all in AUD, so the terminal never shows a profit the wallet did not keep.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .aud import LAMPORTS_PER_SOL

BASE_FEE_LAMPORTS = 5_000


@dataclass
class TxCost:
    """What one transaction cost, in lamports (fees) and AUD (slippage)."""
    compute_units: int = 0
    cu_price_micro_lamports: int = 0
    jito_tip_lamports: int = 0
    signatures: int = 1
    slippage_aud: float = 0.0  # positive = filled worse than quoted

    @property
    def priority_lamports(self) -> float:
        return self.compute_units * self.cu_price_micro_lamports / 1_000_000

    @property
    def fee_lamports(self) -> float:
        return self.signatures * BASE_FEE_LAMPORTS + self.priority_lamports + self.jito_tip_lamports


@dataclass
class Fill:
    """One executed leg: SOL spent or received (before fees) and the costs it paid."""
    side: str  # "buy" or "sell"
    sol: float  # SOL out of the wallet for a buy, SOL into the wallet for a sell (swap amount only)
    tokens: float
    sol_aud: float  # SOL/AUD at the fill
    cost: TxCost = field(default_factory=TxCost)


def leg_costs_aud(f: Fill) -> Dict[str, float]:
    to_aud = f.sol_aud / LAMPORTS_PER_SOL
    return {"base_fee": f.cost.signatures * BASE_FEE_LAMPORTS * to_aud,
            "priority_fee": f.cost.priority_lamports * to_aud,
            "jito_tip": f.cost.jito_tip_lamports * to_aud,
            "slippage": f.cost.slippage_aud}


def net_pnl(fills: List[Fill], mark_sol_aud: Optional[float] = None) -> Dict[str, float]:
    """Gross and net P&L in AUD for one position's fills (realised; open tokens marked at zero unless sold)."""
    spent = sum(f.sol * f.sol_aud for f in fills if f.side == "buy")
    got = sum(f.sol * f.sol_aud for f in fills if f.side == "sell")
    costs = {"base_fee": 0.0, "priority_fee": 0.0, "jito_tip": 0.0, "slippage": 0.0}
    for f in fills:
        for k, v in leg_costs_aud(f).items():
            costs[k] += v
    fees = costs["base_fee"] + costs["priority_fee"] + costs["jito_tip"]
    gross = got - spent
    # Slippage is already inside the fill amounts; it is reported, not subtracted twice.
    net = gross - fees
    out = {"spent_aud": round(spent, 4), "received_aud": round(got, 4), "gross_aud": round(gross, 4),
           **{f"{k}_aud": round(v, 4) for k, v in costs.items()}, "fees_aud": round(fees, 4), "net_aud": round(net, 4)}
    out["net_pct"] = round(100 * net / spent, 3) if spent > 0 else 0.0
    return out


def round_trip_fees_aud(sol_aud: float, compute_units: int = 200_000, cu_price_micro_lamports: int = 100_000,
                        jito_tip_lamports: int = 0) -> float:
    """Expected fees (both legs) in AUD before entering: used to size trades and to skip ones fees would eat."""
    leg = TxCost(compute_units, cu_price_micro_lamports, jito_tip_lamports)
    return 2 * leg.fee_lamports * sol_aud / LAMPORTS_PER_SOL

"""LOKI v8 — the agents behind the live AI's decisions.

    Director ──► Quant ──► Risk

The Director sets the market posture, the Quant produces the trading signal and
the Risk agent sizes or vetoes it. Each emits a structured decision so the whole
chain is auditable. Everything is deterministic: there is no language model here.

Nothing in this module places an order. The API returns the decisions; the paper
ledger that acts on them lives in the visitor's browser.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .config import LokiConfig
from .data import get_provider
from .regime import Regime, is_bearish, is_bullish
from .risk import RiskManager
from .strategy import EnsembleStrategy

logger = logging.getLogger(__name__)


class DirectorAgent:
    """Sets the market posture (long/flat/short) and a one-line thesis."""

    def __init__(self, cfg: LokiConfig):
        self.cfg = cfg

    def run(self, symbol: str, regime: Regime, notes: Optional[str] = None) -> Dict:
        state = regime.state
        if is_bullish(state):
            posture = "long"
            strength = "strong" if state == "STRONG_BULL" else "developing"
            thesis = (
                f"{symbol}: {strength} uptrend "
                f"(trend_score={regime.trend_score:+.0f}, ADX={regime.adx:.0f})"
            )
        elif is_bearish(state):
            posture = "short" if (self.cfg.allow_short and state == "BEAR") else "flat"
            if state == "CRISIS":
                thesis = f"{symbol}: CRISIS regime — high volatility, capital preserved"
            else:
                thesis = (
                    f"{symbol}: downtrend — {'shorting' if posture == 'short' else 'no new longs'}"
                )
        else:
            posture = "long"  # ranging: trade longs on strong signals, tighter size
            thesis = f"{symbol}: ranging market — trade only high-confidence longs"
        if notes:
            thesis = f"{thesis} · {notes}"

        return {
            "symbol": symbol,
            "posture": posture,
            "thesis": thesis,
            "regime": regime.state,
            "confidence": round(regime.confidence, 1),
        }


class QuantAgent:
    """Technical/statistical analysis — wraps the ensemble strategy."""

    def __init__(self, cfg: LokiConfig):
        self.strategy = EnsembleStrategy(cfg)

    def run(self, symbol: str, df) -> Optional[Dict]:
        sig = self.strategy.generate_signal(df, symbol)
        if sig is None:
            return None
        return {
            "symbol": symbol,
            "action": sig.action,
            "score": round(sig.score, 1),
            "confidence": round(sig.confidence, 1),
            "entry": sig.entry_price,
            "stop": sig.stop_loss,
            "target": sig.take_profit,
            "target_2": sig.target_2,
            "risk_reward": sig.risk_reward,
            "squeeze_status": sig.squeeze_status,
            "reasons": sig.reasons,
        }


class RiskAgent:
    """Position sizing + approval, reconciling Director posture with the signal."""

    def __init__(self, cfg: LokiConfig):
        self.cfg = cfg
        self.risk = RiskManager(cfg)

    def run(
        self, posture: str, signal: Optional[Dict], equity: float, open_positions: int, price: float
    ) -> Dict:
        if signal is None:
            return {"approved": False, "reason": "no_signal"}

        # Posture/signal agreement
        if posture == "flat" and signal["action"] == "BUY":
            return {"approved": False, "reason": "director_posture_flat"}
        if posture == "short" and signal["action"] == "BUY":
            return {"approved": False, "reason": "director_posture_short"}
        if posture == "long" and signal["action"] == "SELL":
            # In long posture, SELL is treated as an exit, not a short entry.
            pass

        ok, reason = self.risk.can_trade(equity, open_positions)
        if not ok:
            return {"approved": False, "reason": reason}

        if signal["action"] == "BUY":
            qty = self.risk.position_size(equity, price, signal["stop"])
            # Tighter size in ranging markets
            if posture == "long" and signal.get("regime") == "RANGING":
                qty *= 0.5
            return {
                "approved": qty > 0,
                "reason": "ok",
                "qty": qty,
                "action": "BUY",
                "stop": signal["stop"],
                "target": signal["target"],
            }
        elif signal["action"] == "SELL":
            return {"approved": True, "reason": "ok", "action": "SELL", "qty": 0.0}
        return {"approved": False, "reason": "hold"}


class LokiAgentPipeline:
    """One symbol's Director and Quant, with the price feed they read.

    Risk is deliberately not held here: a pipeline is shared between callers, so each
    request builds its own ``RiskAgent`` from the caller's ledger.
    """

    def __init__(self, cfg: LokiConfig):
        self.cfg = cfg
        self.provider = get_provider()
        self.director = DirectorAgent(cfg)
        self.quant = QuantAgent(cfg)

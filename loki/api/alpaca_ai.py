"""Mirror the Portfolio AI's plan onto an Alpaca paper account (shared, pure logic).

Used by the website's Alpaca panel ("Let the Portfolio AI manage this paper
account") and by alpaca_forward.py's daily forward test, so both trade exactly
the same way: only the AI's markets, nothing under 2% of equity, sells before
buys, stocks only while the US market is open (crypto any day).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

REBALANCE_BAND = 0.02     # same 2%-of-equity band the website's paper Portfolio AI uses
MIN_ORDER_USD = 5.0
# The AI's engine symbols -> Alpaca symbols. BNB is not offered by Alpaca: its share stays in cash.
CRYPTO = {"BTC-USD": "BTC/USD", "ETH-USD": "ETH/USD", "LTC-USD": "LTC/USD", "XRP-USD": "XRP/USD",
          "DOGE-USD": "DOGE/USD", "ADA-USD": "ADA/USD", "LINK-USD": "LINK/USD"}
UNSUPPORTED = {"BNB-USD"}



def alpaca_symbol(engine_symbol: str) -> Optional[str]:
    if engine_symbol in UNSUPPORTED:
        return None
    return CRYPTO.get(engine_symbol, engine_symbol)


def plan_trades(plan: Dict[str, Any], equity: float, held: Dict[str, Dict[str, Any]], stocks_open: bool
                ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Orders that bring the AI's markets to their target weights (pure; unit-tested)."""
    trades: List[Dict[str, Any]] = []
    notes: List[str] = []
    for row in plan.get("positions", []):
        engine = row["symbol"]
        symbol = alpaca_symbol(engine)
        target = max(0.0, float(row.get("weight") or 0.0)) * equity
        if symbol is None:
            if target > 0:
                notes.append(f"{engine}: target ${target:,.0f} kept in cash (not tradable on Alpaca).")
            continue
        crypto = symbol in CRYPTO.values()
        position = held.get(symbol.replace("/", "")) or held.get(symbol) or {}
        current = float(position.get("market_value") or 0.0)
        delta = target - current
        if abs(delta) < max(REBALANCE_BAND * equity, MIN_ORDER_USD):
            continue
        if not crypto and not stocks_open:
            notes.append(f"{symbol}: {'buy' if delta > 0 else 'sell'} about ${abs(delta):,.0f} once the US market opens (it's closed now).")
            continue
        trade = {"symbol": symbol, "engine": engine, "crypto": crypto, "target": round(target, 2),
                 "current": round(current, 2), "delta": round(delta, 2)}
        if delta > 0:
            trade.update(side="buy", notional=round(delta, 2))
        elif target <= 0:
            trade.update(side="close")
        else:
            price = float(position.get("current_price") or 0.0)
            qty = min(float(position.get("qty") or 0.0), -delta / price) if price > 0 else 0.0
            if qty <= 0:
                continue
            trade.update(side="sell", qty=qty)
        trades.append(trade)
    trades.sort(key=lambda t: 0 if t["side"] in ("sell", "close") else 1)  # free cash before buying
    return trades, notes


def performance(history: Dict[str, Any], current_equity: Optional[float] = None) -> Dict[str, Any]:
    """Return and worst drop since the account's first funded day.

    Alpaca's daily history ends at the last completed day, so today's equity
    (``current_equity``, from the account) is appended to keep the figures in
    step with the balance shown next to them.
    """
    equity = [float(e) for e in (history.get("equity") or []) if e is not None and float(e) > 0]
    stamps = history.get("timestamp") or []
    start = datetime.fromtimestamp(stamps[-len(equity)], tz=timezone.utc).date() if stamps and equity else None
    if current_equity and current_equity > 0 and equity and abs(current_equity - equity[-1]) > 0.005:
        equity.append(float(current_equity))
    if len(equity) < 2:
        return {"days": len(equity)}
    peak, worst = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return {"days": len(equity), "since": str(start), "start_equity": equity[0], "equity": equity[-1],
            "return_pct": round((equity[-1] / equity[0] - 1) * 100, 2), "max_drawdown_pct": round(worst * 100, 2)}



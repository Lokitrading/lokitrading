"""Forward-test the Portfolio AI on an Alpaca PAPER account.

Each run:
1. builds the Portfolio AI's current plan (the same code the website serves),
2. compares it with the paper account's holdings,
3. places only the trades needed to match it (moves under 2% of equity are skipped),
4. prints, and in GitHub Actions summarises, the account's performance.

Safety:
* Paper only — the trading host is hard-wired to paper-api.alpaca.markets.
* Keys come from the environment (APCA_API_KEY_ID / APCA_API_SECRET_KEY), never arguments or files.
* Dry run unless --execute is given.
* Only markets in the AI's universe are touched; anything else in the account is left alone.

Usage:
    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python alpaca_forward.py            # dry run
    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python alpaca_forward.py --execute  # place paper orders
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

PAPER_URL = "https://paper-api.alpaca.markets"
REBALANCE_BAND = 0.02     # same 2%-of-equity band the website's paper Portfolio AI uses
MIN_ORDER_USD = 5.0
# The AI's engine symbols -> Alpaca symbols. BNB is not offered by Alpaca: its share stays in cash.
CRYPTO = {"BTC-USD": "BTC/USD", "ETH-USD": "ETH/USD", "LTC-USD": "LTC/USD", "XRP-USD": "XRP/USD",
          "DOGE-USD": "DOGE/USD", "ADA-USD": "ADA/USD", "LINK-USD": "LINK/USD"}
UNSUPPORTED = {"BNB-USD"}


class Alpaca:
    def __init__(self, key: str, secret: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, PAPER_URL + path, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else None

    def account(self) -> Dict[str, Any]:
        return self._call("GET", "/v2/account")

    def clock(self) -> Dict[str, Any]:
        return self._call("GET", "/v2/clock")

    def positions(self) -> Dict[str, Dict[str, Any]]:
        return {p["symbol"]: p for p in self._call("GET", "/v2/positions")}

    def history(self) -> Dict[str, Any]:
        return self._call("GET", "/v2/account/portfolio/history", params={"period": "1A", "timeframe": "1D"})

    def order(self, symbol: str, side: str, notional: Optional[float] = None, qty: Optional[float] = None,
              crypto: bool = False) -> Dict[str, Any]:
        body: Dict[str, Any] = {"symbol": symbol, "side": side, "type": "market",
                                "time_in_force": "gtc" if crypto else "day"}
        if qty is not None:
            body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
        else:
            body["notional"] = f"{notional:.2f}"
        return self._call("POST", "/v2/orders", json=body)

    def close(self, symbol: str) -> Dict[str, Any]:
        return self._call("DELETE", "/v2/positions/" + symbol.replace("/", ""))


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
            notes.append(f"{symbol}: wants {'+' if delta > 0 else '-'}${abs(delta):,.0f}; US market closed, next run will trade it.")
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


def performance(history: Dict[str, Any]) -> Dict[str, Any]:
    equity = [float(e) for e in (history.get("equity") or []) if e is not None and float(e) > 0]
    stamps = history.get("timestamp") or []
    if len(equity) < 2:
        return {"days": len(equity)}
    peak, worst = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    start = datetime.fromtimestamp(stamps[-len(equity)], tz=timezone.utc).date() if stamps else None
    return {"days": len(equity), "since": str(start), "start_equity": equity[0], "equity": equity[-1],
            "return_pct": round((equity[-1] / equity[0] - 1) * 100, 2), "max_drawdown_pct": round(worst * 100, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="place paper orders (default: dry run)")
    args = parser.parse_args()
    key, secret = os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        print("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY (Alpaca PAPER keys).", file=sys.stderr)
        return 2

    from loki.api import ai_paper  # imported late so --help works without the data stack

    broker = Alpaca(key, secret)
    account = broker.account()
    equity = float(account["equity"])
    stocks_open = bool(broker.clock().get("is_open"))
    plan = ai_paper._portfolio_plan()
    trades, notes = plan_trades(plan, equity, broker.positions(), stocks_open)

    lines = [f"## Portfolio AI forward test — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"Alpaca PAPER account · equity ${equity:,.2f} · US market {'open' if stocks_open else 'closed'} · "
             f"plan as of {plan.get('as_of')} · AI invested {plan.get('gross', 0) * 100:.0f}%", ""]
    held_targets = [r for r in plan.get("positions", []) if r.get("weight", 0) > 0]
    lines.append("Target: " + (", ".join(f"{r['symbol']} {r['weight'] * 100:.1f}%" for r in held_targets) or "all cash"))
    placed = 0
    for t in trades:
        what = (f"buy ${t['notional']:,.2f}" if t["side"] == "buy" else
                "close position" if t["side"] == "close" else f"sell {t['qty']:.6g}")
        status = "dry run"
        if args.execute:
            try:
                if t["side"] == "close":
                    broker.close(t["symbol"])
                else:
                    broker.order(t["symbol"], t["side"], notional=t.get("notional"), qty=t.get("qty"), crypto=t["crypto"])
                status, placed = "placed", placed + 1
            except RuntimeError as error:
                status = f"FAILED: {error}"
        lines.append(f"- {t['symbol']}: {what} (now ${t['current']:,.0f} → target ${t['target']:,.0f}) — {status}")
    if not trades and not notes:
        lines.append("- No trades needed: holdings are within 2% of the plan.")
    elif not trades:
        lines.append("- Nothing to place right now:")
    lines += [f"- {n}" for n in notes]
    perf = performance(broker.history())
    if perf.get("days", 0) >= 2:
        lines.append("")
        lines.append(f"Account since {perf['since']}: {perf['return_pct']:+.2f}% "
                     f"(${perf['start_equity']:,.0f} → ${perf['equity']:,.0f}), worst drop {perf['max_drawdown_pct']:.2f}%. "
                     "Paper trading — judge it over months, not days.")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    failed = any("FAILED" in line for line in lines)
    return 1 if failed and not placed else 0


if __name__ == "__main__":
    sys.exit(main())

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
from typing import Any, Dict, Optional

import requests

from loki.api.alpaca_ai import (  # noqa: F401 - re-exported for tests
    CRYPTO, MIN_ORDER_USD, REBALANCE_BAND, UNSUPPORTED, alpaca_symbol, performance, plan_trades,
)

PAPER_URL = "https://paper-api.alpaca.markets"
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
    perf = performance(broker.history(), equity)
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

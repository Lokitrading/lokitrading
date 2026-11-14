"""Phase 1 shadow mode for new Solana memecoins: screen live launches, simulate trades, log everything.

Paper only. It never holds a private key and never signs or sends a transaction.

Every ``--every`` seconds it:

1. reads new launches (RugCheck's new-token feed) and recently promoted tokens (DexScreener);
2. screens each one with the safety filters in ``loki_v8/launch_filters.py`` (authorities revoked,
   liquidity locked or a bonding curve, top-10 holders at most 25%, no insider network, no
   danger-level RugCheck risk) and, for tokens that pass, checks how much supply was bundled in the
   launch slots straight from Solana's own transaction history;
3. enters on organic velocity (5-minute volume at least 2x liquidity, buys at least 1.5x sells) with
   a simulated position priced from a real Jupiter quote, so price impact and pool fees are real;
4. marks open positions with real Jupiter sell quotes and exits them with the laddered take-profits,
   trailing moonbag, time stop, hard stop and holder-sell triggers;
5. charges ``--tx-cost`` SOL (priority fee and tip) for every simulated transaction;
6. writes tokens, verdicts and trades to a SQLite database.

What it cannot measure, and the report says so: transactions that fail or land late during a
spike, MEV against a real order, and wash trading by one wallet (DexScreener counts transactions,
not unique wallets). Real results would be worse by those amounts.

Usage:
    python memecoin_shadow.py --minutes 120 --db shadow.db     # run for two hours (0 = until stopped)
    python memecoin_shadow.py --report --db shadow.db          # what it found so far
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from loki_v8 import launch_filters as lf

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "LokiShadowScout/1.0", "Accept": "application/json"}
WATCH_MINUTES = 120  # a token is re-checked for velocity for this long after it is first seen
MAX_OPEN = 5
SIGNATURE_PAGES = 3


def http(url: str, body: Optional[Dict[str, Any]] = None, tries: int = 3) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {**UA, **({"Content-Type": "application/json"} if data else {})}
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as res:  # noqa: S310
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 + 3 * attempt)
                continue
            return None
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1 + attempt)
    return None


def rpc(method: str, params: List[Any]) -> Any:
    answer = http(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    time.sleep(0.12)  # stay under the public endpoint's rate limit
    return (answer or {}).get("result")


def quote(input_mint: str, output_mint: str, amount: int) -> Optional[Dict[str, Any]]:
    if amount <= 0:
        return None
    q = http(f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps=300")
    return q if isinstance(q, dict) and q.get("outAmount") else None


def sol_usd() -> Optional[float]:
    q = quote(SOL, USDC, 1_000_000_000)
    return int(q["outAmount"]) / 1e6 if q else None


def discover() -> List[str]:
    mints: List[str] = []
    for item in http("https://api.rugcheck.xyz/v1/stats/new_tokens") or []:
        if isinstance(item, dict) and item.get("mint"):
            mints.append(item["mint"])
    for item in http("https://api.dexscreener.com/token-profiles/latest/v1") or []:
        if isinstance(item, dict) and item.get("chainId") == "solana" and item.get("tokenAddress"):
            mints.append(item["tokenAddress"])
    return list(dict.fromkeys(mints))


def bundle_check(mint: str, pools: List[str]) -> Optional[float]:
    """Share of supply bought outside pools in the first two slots, from Solana's own history."""
    supply = rpc("getTokenSupply", [mint])
    total = lf._num(((supply or {}).get("value") or {}).get("uiAmount"))
    signatures: List[Dict[str, Any]] = []
    before = None
    for _ in range(SIGNATURE_PAGES):
        page = rpc("getSignaturesForAddress", [mint, {"limit": 1000, **({"before": before} if before else {})}])
        if not page:
            break
        signatures += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    else:
        return None  # more history than we read: the launch slots are out of reach
    if not signatures or not total:
        return None
    first = sorted({s["slot"] for s in signatures})[:2]
    early = [s["signature"] for s in signatures if s["slot"] in first][:25]
    txs = [t for t in (rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) for sig in early) if t]
    return lf.launch_bundle_pct(txs, mint, total, pools)


def holder_balances(accounts: List[str]) -> Dict[str, float]:
    out = {}
    for account in accounts:
        value = (rpc("getTokenAccountBalance", [account]) or {}).get("value") or {}
        amount = lf._num(value.get("uiAmount"))
        if amount is not None:
            out[account] = amount
    return out


# --- storage ---------------------------------------------------------------------------------
SCHEMA = """
create table if not exists tokens (mint text primary key, symbol text, first_seen real, last_checked real,
  verdict text, reasons text, figures text);
create table if not exists trades (id integer primary key, mint text, symbol text, opened_at real, entry_usd real,
  entry_price real, tokens real, decimals integer, holders text, closed_at real, proceeds_usd real, costs_usd real,
  pnl_usd real, peak real, exits text, state text);
"""


def db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def record_token(con: sqlite3.Connection, mint: str, symbol: str, verdict: str, reasons: List[str], figures: Dict[str, Any]) -> None:
    now = time.time()
    con.execute("insert into tokens values (?,?,?,?,?,?,?) on conflict(mint) do update set last_checked=excluded.last_checked, "
                "verdict=excluded.verdict, reasons=excluded.reasons, figures=excluded.figures, symbol=excluded.symbol",
                (mint, symbol, now, now, verdict, json.dumps(reasons), json.dumps(figures)))
    con.commit()


# --- one pass -----------------------------------------------------------------------------------
def screen(con: sqlite3.Connection, mint: str, sol_price: float, size_usd: float, tx_cost_sol: float) -> None:
    row = con.execute("select first_seen, verdict from tokens where mint=?", (mint,)).fetchone()
    if row and (row[1] in ("rejected", "entered") or time.time() - row[0] > WATCH_MINUTES * 60):
        return
    report = http(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    if not isinstance(report, dict):
        return
    symbol = str((report.get("tokenMeta") or {}).get("symbol") or (report.get("token") or {}).get("symbol") or "")[:20]
    reasons = lf.safety(report)
    if reasons:
        record_token(con, mint, symbol, "rejected", reasons, {})
        return
    pairs = http(f"https://api.dexscreener.com/tokens/v1/solana/{mint}") or []
    pair = max(pairs, key=lambda p: lf._num((p.get("volume") or {}).get("m5")) or 0) if pairs else None
    waits, figures = lf.velocity(pair, lf._num(report.get("totalMarketLiquidity")))
    if waits:
        record_token(con, mint, symbol, "watching", waits, figures)
        return
    pools = [m.get("pubkey") for m in report.get("markets") or [] if m.get("pubkey")]
    bundle = bundle_check(mint, pools)
    reasons = lf.safety(report, launch_bundle_pct=bundle)
    if bundle is None:
        reasons.append("launch slots could not be read, so bundling cannot be ruled out")
    if reasons:
        record_token(con, mint, symbol, "rejected", reasons, {**figures, "launch_bundle_pct": bundle})
        return
    if con.execute("select count(*) from trades where state='open'").fetchone()[0] >= MAX_OPEN:
        record_token(con, mint, symbol, "watching", [f"already {MAX_OPEN} positions open"], figures)
        return
    lamports = int(size_usd / sol_price * 1e9)
    q = quote(SOL, mint, lamports)
    if not q:
        record_token(con, mint, symbol, "watching", ["no Jupiter route to buy"], figures)
        return
    decimals = int(((report.get("token") or {}).get("decimals")) or 6)
    tokens = int(q["outAmount"]) / 10 ** decimals
    entry_price = size_usd / tokens
    tracked = [h["address"] for h in lf.holders_outside_pools(report)[:5] if h.get("address")]
    known = report.get("knownAccounts") or {}
    tracked += [h["address"] for h in report.get("topHolders") or [] if (known.get(h.get("owner")) or {}).get("type") == "CREATOR" and h.get("address")]
    holders = holder_balances(list(dict.fromkeys(tracked)))
    con.execute("insert into trades (mint, symbol, opened_at, entry_usd, entry_price, tokens, decimals, holders, proceeds_usd, costs_usd, peak, exits, state) "
                "values (?,?,?,?,?,?,?,?,0,?,1,'[]','open')",
                (mint, symbol, time.time(), size_usd, entry_price, tokens, decimals, json.dumps(holders), tx_cost_sol * sol_price))
    record_token(con, mint, symbol, "entered", [], {**figures, "launch_bundle_pct": bundle, "price_impact_pct": lf._num(q.get("priceImpactPct"))})


def manage(con: sqlite3.Connection, sol_price: float, plan: lf.ExitPlan, tx_cost_sol: float) -> None:
    rows = con.execute("select id, mint, opened_at, entry_usd, entry_price, tokens, decimals, holders, proceeds_usd, costs_usd, peak, exits "
                       "from trades where state='open'").fetchall()
    for tid, mint, opened, entry_usd, entry_price, tokens, decimals, holders_json, proceeds, costs, peak, exits_json in rows:
        exits = json.loads(exits_json)
        sold = sum(e["share"] for e in exits)
        remaining = max(0.0, 1.0 - sold)
        q = quote(mint, SOL, int(tokens * remaining * 10 ** decimals))
        misses = sum(1 for e in exits if e.get("no_route"))
        if not q:
            exits.append({"no_route": True, "at": time.time(), "share": 0.0})
            if misses + 1 >= 3:  # three checks with no way out: counted as a total loss of what is left
                exits.append({"share": remaining, "usd": 0.0, "reason": "no route to sell: counted as lost", "at": time.time()})
                con.execute("update trades set exits=?, state='closed', closed_at=?, pnl_usd=?-?-? where id=?",
                            (json.dumps(exits), time.time(), proceeds, entry_usd, costs, tid))
            else:
                con.execute("update trades set exits=? where id=?", (json.dumps(exits), tid))
            con.commit()
            continue
        value_usd = int(q["outAmount"]) / 1e9 * sol_price
        price = value_usd / (tokens * remaining) if remaining > 0 else 0.0
        pos = lf.Position(mint, entry_price, opened, remaining=remaining, peak=peak,
                          taken=[e["level"] for e in exits if e.get("level")], holders=json.loads(holders_json))
        balances = holder_balances(list(pos.holders)) if pos.holders else None
        for share, reason in lf.step(pos, price, time.time(), plan, balances):
            usd = value_usd * share / remaining if remaining else 0.0
            level = next((lvl for lvl, _ in plan.take_profits if reason == f"take profit at {lvl}x"), None)
            exits.append({"share": share, "usd": round(usd, 4), "reason": reason, "at": time.time(), "multiple": round(price / entry_price, 3), "level": level})
            proceeds += usd
            costs += tx_cost_sol * sol_price
        state = "closed" if pos.remaining <= 1e-9 else "open"
        con.execute("update trades set exits=?, proceeds_usd=?, costs_usd=?, peak=?, state=?, closed_at=?, pnl_usd=? where id=?",
                    (json.dumps(exits), proceeds, costs, pos.peak, state, time.time() if state == "closed" else None,
                     proceeds - entry_usd - costs if state == "closed" else None, tid))
        con.commit()


def report(con: sqlite3.Connection) -> Dict[str, Any]:
    tokens = con.execute("select verdict, reasons from tokens").fetchall()
    reasons: Dict[str, int] = {}
    for verdict, r in tokens:
        if verdict == "rejected":
            for reason in json.loads(r):
                key = (reason if reason.startswith("RugCheck:") else "top 10 holders own too much" if reason.startswith("top 10")
                       else re.sub(r"^\d+ ", "", reason.split("(")[0].split(":")[0]).strip())
                reasons[key] = reasons.get(key, 0) + 1
    closed = con.execute("select pnl_usd, entry_usd, peak, exits from trades where state='closed'").fetchall()
    pnl = [c[0] for c in closed]
    opened = con.execute("select count(*) from trades where state='open'").fetchone()[0]
    return {
        "tokens_screened": len(tokens),
        "rejected": sum(1 for v, _ in tokens if v == "rejected"), "watching": sum(1 for v, _ in tokens if v == "watching"),
        "entered": sum(1 for v, _ in tokens if v == "entered"),
        "top_rejection_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:8]),
        "trades_closed": len(closed), "trades_open": opened,
        "net_pnl_usd": round(sum(pnl), 2), "expectancy_usd": round(sum(pnl) / len(pnl), 3) if pnl else None,
        "win_rate_pct": round(sum(1 for p in pnl if p > 0) / len(pnl) * 100, 1) if pnl else None,
        "best_usd": round(max(pnl), 2) if pnl else None, "worst_usd": round(min(pnl), 2) if pnl else None,
        "phase_2_gate": "met" if len(pnl) >= 200 and sum(pnl) > 0 else f"not met: needs 200+ closed trades with positive net result ({len(pnl)} so far)",
        "not_measured": ["failed or late transactions in a spike", "MEV against a real order", "wash trading by one wallet"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("memecoin_shadow.db"))
    parser.add_argument("--minutes", type=float, default=60.0, help="how long to run (0 = until stopped)")
    parser.add_argument("--every", type=float, default=20.0, help="seconds between passes")
    parser.add_argument("--size-usd", type=float, default=20.0, help="simulated position size")
    parser.add_argument("--tx-cost", type=float, default=0.0003, help="SOL per simulated transaction (priority fee + tip)")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    con = db(args.db)
    if args.report:
        print(json.dumps(report(con), indent=1))
        return
    plan, end = lf.ExitPlan(), time.time() + args.minutes * 60 if args.minutes else float("inf")
    while time.time() < end:
        started = time.time()
        price = sol_usd()
        if price:
            manage(con, price, plan, args.tx_cost)
            for mint in discover():
                try:
                    screen(con, mint, price, args.size_usd, args.tx_cost)
                except (KeyError, TypeError, ValueError) as error:  # one malformed answer skips one token, never the loop
                    print("skip", mint, type(error).__name__)
            summary = report(con)
            print(time.strftime("%H:%M:%S"), {k: summary[k] for k in ("tokens_screened", "rejected", "entered", "trades_open", "trades_closed", "net_pnl_usd")}, flush=True)
        time.sleep(max(1.0, args.every - (time.time() - started)))
    print(json.dumps(report(con), indent=1))


if __name__ == "__main__":
    main()

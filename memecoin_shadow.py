"""Phase 1 shadow mode for new Solana memecoins: screen live launches, simulate trades, log everything.

Paper only. It never holds a private key and never signs or sends a transaction.

Every ``--every`` seconds it:

1. reads new launches (RugCheck's new-token feed) and recently promoted tokens (DexScreener);
2. screens each with ``loki_v8/launch_filters.py`` (authorities revoked, liquidity locked or a
   bonding curve, top-10 holders at most 25%, no insider network, no danger-level RugCheck risk, no
   transfer fee); tokens that pass and show organic velocity get the deeper checks straight from
   Solana: supply bundled in the launch slots, and launch buyers funded by one shared wallet;
3. before entering, "simulates before signing": it asks Jupiter for a buy and then for selling what
   that buy returns. A round trip that loses more than 10% points to a hidden tax or a honeypot;
4. enters through a strict state machine: PENDING_ENTRY at the decision quote, then a second quote
   ``--latency`` seconds later stands in for the moment a real transaction lands. If the price moved
   more than 3% against us, the entry is FAILED_SAFE (paying only the transaction fee), as a real
   order with a 3% slippage cap would fail;
5. marks open positions with real Jupiter sell quotes and exits them with the laddered take-profits,
   trailing moonbag, time stop, hard stop and holder-sell triggers (OPEN, PARTIAL_EXIT, CLOSED);
6. charges ``--tx-cost`` SOL for every simulated transaction, failed ones included;
7. writes tokens, verdicts and trades to SQLite, and every trade event to a hash-chained ledger
   that ``--verify`` re-checks.

Guards: Solana calls fail over across every endpoint in ``SOLANA_RPC_URLS`` (comma-separated) and
the public one, each taken out for a minute after it fails; new entries pause while more than half
of the recent outside calls fail; and a daily kill switch stops new entries once the day's closed
trades lose ``--kill-usd``. A position stuck in PENDING_ENTRY longer than a minute is rolled back
to FAILED_SAFE.

What it still cannot measure: MEV against a real order, and wash trading by one wallet (DexScreener
counts transactions, not wallets). Real results would be worse by those amounts.

Usage:
    python memecoin_shadow.py --minutes 120 --db shadow.db     # run for two hours (0 = until stopped)
    python memecoin_shadow.py --report --db shadow.db          # what it found so far
    python memecoin_shadow.py --verify --db shadow.db          # re-check the ledger's hash chain
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from loki_v8 import launch_filters as lf

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "LokiShadowScout/1.0", "Accept": "application/json"}
WATCH_MINUTES = 120  # a token is re-checked for velocity for this long after it is first seen
MAX_OPEN = 5
SIGNATURE_PAGES = 3
MAX_FUNDER_LOOKUPS = 8
PENDING_TIMEOUT_S = 60
BREAKER_WINDOW, BREAKER_MIN_CALLS, BREAKER_FAIL_RATE = 60, 20, 0.5
ENDPOINT_COOLDOWN_S = 60

_calls: Deque[bool] = collections.deque(maxlen=BREAKER_WINDOW)  # recent outside calls: True = answered
_down_until: Dict[str, float] = {}


def rpc_urls() -> List[str]:
    extra = [u.strip() for u in os.environ.get("SOLANA_RPC_URLS", "").split(",") if u.strip().startswith("https://")]
    return list(dict.fromkeys(extra + [PUBLIC_RPC]))


def breaker_open() -> bool:
    """True while more than half of the recent outside calls failed: no new entries, exits continue."""
    return len(_calls) >= BREAKER_MIN_CALLS and _calls.count(False) / len(_calls) > BREAKER_FAIL_RATE


def http(url: str, body: Optional[Dict[str, Any]] = None, tries: int = 3) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {**UA, **({"Content-Type": "application/json"} if data else {})}
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as res:  # noqa: S310
                answer = json.loads(res.read().decode())
            _calls.append(True)
            return answer
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 + 3 * attempt)
                continue
            break
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1 + attempt)
    _calls.append(False)
    return None


def rpc(method: str, params: List[Any]) -> Any:
    """One Solana call, failing over across endpoints; an endpoint that fails sits out for a minute."""
    for url in rpc_urls():
        if _down_until.get(url, 0) > time.time():
            continue
        answer = http(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, tries=1)
        time.sleep(0.12)  # stay under public rate limits
        if isinstance(answer, dict) and "result" in answer:
            return answer["result"]
        _down_until[url] = time.time() + ENDPOINT_COOLDOWN_S
    return None


def quote(input_mint: str, output_mint: str, amount: int) -> Optional[Dict[str, Any]]:
    if amount <= 0:
        return None
    q = http(f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps=300")
    return q if isinstance(q, dict) and str(q.get("outAmount") or "").isdigit() and int(q["outAmount"]) > 0 else None


def sol_usd() -> Optional[float]:
    q = quote(SOL, USDC, 1_000_000_000)
    return int(q["outAmount"]) / 1e6 if q else None


def discover() -> List[str]:
    mints: List[str] = []
    for item in http("https://api.rugcheck.xyz/v1/stats/new_tokens") or []:
        if isinstance(item, dict) and isinstance(item.get("mint"), str):
            mints.append(item["mint"])
    for item in http("https://api.dexscreener.com/token-profiles/latest/v1") or []:
        if isinstance(item, dict) and item.get("chainId") == "solana" and isinstance(item.get("tokenAddress"), str):
            mints.append(item["tokenAddress"])
    return list(dict.fromkeys(mints))


def launch_check(mint: str, pools: List[str]) -> Tuple[Optional[float], Optional[Tuple[str, int]]]:
    """(share of supply bought outside pools in the first two slots, the shared funder of launch buyers)."""
    supply = rpc("getTokenSupply", [mint])
    total = lf._num(((supply or {}).get("value") or {}).get("uiAmount"))
    signatures: List[Dict[str, Any]] = []
    before = None
    for _ in range(SIGNATURE_PAGES):
        page = rpc("getSignaturesForAddress", [mint, {"limit": 1000, **({"before": before} if before else {})}])
        if not isinstance(page, list) or not page:
            break
        signatures += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    else:
        return None, None  # more history than we read: the launch slots are out of reach
    if not signatures or not total:
        return None, None
    first = sorted({s["slot"] for s in signatures})[:2]
    early = [s["signature"] for s in signatures if s["slot"] in first][:25]
    txs = [t for t in (rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) for sig in early) if t]
    buyers = lf.launch_buyers(txs, mint, pools)
    funders: Dict[str, Optional[str]] = {}
    for wallet in sorted(buyers, key=lambda w: -buyers[w])[:MAX_FUNDER_LOOKUPS]:
        history = rpc("getSignaturesForAddress", [wallet, {"limit": 1000}])
        if not isinstance(history, list) or not history or len(history) >= 1000:
            continue  # an old, busy wallet is not a fresh launch wallet
        first_tx = rpc("getTransaction", [history[-1]["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        funders[wallet] = lf.funder_of(first_tx, wallet)
    return round(sum(buyers.values()) / total * 100, 2), lf.shared_funder(funders)


def holder_balances(accounts: List[str]) -> Dict[str, float]:
    out = {}
    for account in accounts:
        value = (rpc("getTokenAccountBalance", [account]) or {}).get("value") or {}
        amount = lf._num(value.get("uiAmount"))
        if amount is not None:
            out[account] = amount
    return out


# --- storage and the ledger ----------------------------------------------------------------------
SCHEMA = """
create table if not exists tokens (mint text primary key, symbol text, first_seen real, last_checked real,
  verdict text, reasons text, figures text);
create table if not exists trades (id integer primary key, mint text, symbol text, opened_at real, entry_usd real,
  entry_price real, tokens real, decimals integer, holders text, closed_at real, proceeds_usd real, costs_usd real,
  pnl_usd real, peak real, exits text, state text);
create table if not exists ledger (seq integer primary key, at real, kind text, body text, prev text, hash text);
"""


def db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("update trades set state='OPEN' where state='open'")  # databases from 2.77
    con.execute("update trades set state='CLOSED' where state='closed'")
    con.commit()
    return con


def _digest(seq: int, at: float, kind: str, body: str, prev: str) -> str:
    return hashlib.sha256(f"{seq}|{at:.3f}|{kind}|{body}|{prev}".encode()).hexdigest()


def ledger(con: sqlite3.Connection, kind: str, body: Dict[str, Any]) -> str:
    """Append one event to the hash chain; each entry's hash covers the one before it."""
    row = con.execute("select seq, hash from ledger order by seq desc limit 1").fetchone()
    seq, prev = (row[0] + 1, row[1]) if row else (1, "genesis")
    at, text = round(time.time(), 3), json.dumps(body, sort_keys=True, default=str)
    digest = _digest(seq, at, kind, text, prev)
    con.execute("insert into ledger values (?,?,?,?,?,?)", (seq, at, kind, text, prev, digest))
    con.commit()
    return digest


def verify(con: sqlite3.Connection) -> Dict[str, Any]:
    prev = "genesis"
    rows = con.execute("select seq, at, kind, body, prev, hash from ledger order by seq").fetchall()
    for i, (seq, at, kind, body, p, h) in enumerate(rows, start=1):
        if seq != i or p != prev or _digest(seq, at, kind, body, p) != h:
            return {"ok": False, "entries": len(rows), "broken_at": seq}
        prev = h
    return {"ok": True, "entries": len(rows), "head": prev}


def record_token(con: sqlite3.Connection, mint: str, symbol: str, verdict: str, reasons: List[str], figures: Dict[str, Any]) -> None:
    now = time.time()
    con.execute("insert into tokens values (?,?,?,?,?,?,?) on conflict(mint) do update set last_checked=excluded.last_checked, "
                "verdict=excluded.verdict, reasons=excluded.reasons, figures=excluded.figures, symbol=excluded.symbol",
                (mint, symbol, now, now, verdict, json.dumps(reasons), json.dumps(figures, default=str)))
    con.commit()


TRADE_FIELDS = {"entry_price", "tokens", "holders", "closed_at", "proceeds_usd", "costs_usd", "pnl_usd", "peak", "exits"}


def set_state(con: sqlite3.Connection, trade_id: int, new: str, **fields: Any) -> None:
    """Move a trade to ``new`` through the state machine (an illegal move raises), updating ``fields``."""
    if not set(fields) <= TRADE_FIELDS:
        raise ValueError(f"unknown trade fields {set(fields) - TRADE_FIELDS}")
    current = con.execute("select state from trades where id=?", (trade_id,)).fetchone()[0]
    lf.advance(current, new)
    cols = "".join(f", {k}=?" for k in fields)
    con.execute(f"update trades set state=?{cols} where id=?", (new, *fields.values(), trade_id))  # noqa: S608 - column names from TRADE_FIELDS
    con.commit()


def day_pnl(con: sqlite3.Connection) -> float:
    start = time.time() - time.time() % 86400  # UTC midnight
    return float(con.execute("select coalesce(sum(pnl_usd), 0) from trades where closed_at >= ?", (start,)).fetchone()[0])


# --- one pass -----------------------------------------------------------------------------------
def screen(con: sqlite3.Connection, mint: str, sol_price: float, size_usd: float, tx_cost_sol: float,
           latency_s: float = 2.0, kill_usd: float = 100.0) -> None:
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
    pairs = http(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
    pairs = [p for p in pairs if isinstance(p, dict)] if isinstance(pairs, list) else []
    pair = max(pairs, key=lambda p: lf._num((p.get("volume") or {}).get("m5")) or 0) if pairs else None
    waits, figures = lf.velocity(pair, lf._num(report.get("totalMarketLiquidity")))
    if waits:
        record_token(con, mint, symbol, "watching", waits, figures)
        return
    open_now = con.execute("select count(*) from trades where state in ('PENDING_ENTRY','OPEN','PARTIAL_EXIT')").fetchone()[0]
    for blocked, why in ((breaker_open(), "outside services failing: new entries paused"),
                         (day_pnl(con) <= -kill_usd, f"daily kill switch: today's closed trades lost ${kill_usd:.0f} or more"),
                         (open_now >= MAX_OPEN, f"already {MAX_OPEN} positions open")):
        if blocked:
            record_token(con, mint, symbol, "watching", [why], figures)
            return
    pools = [str(m["pubkey"]) for m in lf._list(report.get("markets")) if isinstance(m, dict) and m.get("pubkey")]
    bundle, cabal = launch_check(mint, pools)
    reasons = lf.safety(report, launch_bundle_pct=bundle, cabal=cabal)
    if bundle is None:
        reasons.append("launch slots could not be read, so bundling cannot be ruled out")
    figures = {**figures, "launch_bundle_pct": bundle, "shared_funder": cabal}
    if reasons:
        record_token(con, mint, symbol, "rejected", reasons, figures)
        return
    lamports = int(size_usd / sol_price * 1e9)
    decided = quote(SOL, mint, lamports)
    back = quote(mint, SOL, int(decided["outAmount"])) if decided else None
    if not decided or not back:
        record_token(con, mint, symbol, "watching", ["no Jupiter route both ways"], figures)
        return
    round_trip = 1 - int(back["outAmount"]) / lamports
    if round_trip > lf.MAX_ROUND_TRIP_LOSS:
        record_token(con, mint, symbol, "rejected", [f"buying and selling at once loses {round_trip:.0%}: a hidden tax or a honeypot"],
                     {**figures, "round_trip_loss": round(round_trip, 4)})
        return
    decimals = int(((report.get("token") or {}).get("decimals")) or 6)
    known = report.get("knownAccounts") or {}
    tracked = [h["address"] for h in lf.holders_outside_pools(report)[:5] if h.get("address")]
    tracked += [h["address"] for h in report.get("topHolders") or [] if (known.get(h.get("owner")) or {}).get("type") == "CREATOR" and h.get("address")]
    cur = con.execute("insert into trades (mint, symbol, opened_at, entry_usd, decimals, holders, proceeds_usd, costs_usd, peak, exits, state) "
                      "values (?,?,?,?,?,?,0,?,1,'[]','PENDING_ENTRY')",
                      (mint, symbol, time.time(), size_usd, decimals, "{}", tx_cost_sol * sol_price))
    con.commit()
    trade_id = int(cur.lastrowid or 0)
    ledger(con, "pending_entry", {"trade": trade_id, "mint": mint, "usd": size_usd, "decided_out": decided["outAmount"], "round_trip_loss": round(round_trip, 4)})
    time.sleep(latency_s)  # the time a real transaction would take to land
    landed = quote(SOL, mint, lamports)
    slip = 1 - int(landed["outAmount"]) / int(decided["outAmount"]) if landed else None
    if landed is None or slip is None or slip > lf.MAX_LANDING_SLIP:
        why = "no route when the order would land" if slip is None else f"price moved {slip:.1%} against us before landing (cap {lf.MAX_LANDING_SLIP:.0%})"
        cost = tx_cost_sol * sol_price
        set_state(con, trade_id, "FAILED_SAFE", closed_at=time.time(), pnl_usd=-cost, exits=json.dumps([{"reason": why}]))
        ledger(con, "failed_safe", {"trade": trade_id, "why": why, "cost_usd": round(cost, 4)})
        record_token(con, mint, symbol, "entered", [why], {**figures, "landing_slip": slip})
        return
    tokens = int(landed["outAmount"]) / 10 ** decimals
    holders = holder_balances(list(dict.fromkeys(tracked)))
    set_state(con, trade_id, "OPEN", entry_price=size_usd / tokens, tokens=tokens, holders=json.dumps(holders))
    ledger(con, "open", {"trade": trade_id, "mint": mint, "tokens": tokens, "entry_price": size_usd / tokens, "landing_slip": round(slip, 4)})
    record_token(con, mint, symbol, "entered", [], {**figures, "round_trip_loss": round(round_trip, 4), "landing_slip": round(slip, 4)})


def manage(con: sqlite3.Connection, sol_price: float, plan: lf.ExitPlan, tx_cost_sol: float) -> None:
    for (tid,) in con.execute("select id from trades where state='PENDING_ENTRY' and opened_at < ?", (time.time() - PENDING_TIMEOUT_S,)).fetchall():
        set_state(con, tid, "FAILED_SAFE", closed_at=time.time(), pnl_usd=0.0, exits=json.dumps([{"reason": "stuck pending: rolled back"}]))
        ledger(con, "failed_safe", {"trade": tid, "why": "stuck pending: rolled back"})
    rows = con.execute("select id, mint, opened_at, entry_usd, entry_price, tokens, decimals, holders, proceeds_usd, costs_usd, peak, exits "
                       "from trades where state in ('OPEN','PARTIAL_EXIT')").fetchall()
    for tid, mint, opened, entry_usd, entry_price, tokens, decimals, holders_json, proceeds, costs, peak, exits_json in rows:
        exits = json.loads(exits_json)
        remaining = max(0.0, 1.0 - sum(e.get("share", 0.0) for e in exits))
        q = quote(mint, SOL, int(tokens * remaining * 10 ** decimals))
        if not q:
            exits.append({"no_route": True, "at": time.time(), "share": 0.0})
            if sum(1 for e in exits if e.get("no_route")) >= 3:  # three checks with no way out: what is left counts as lost
                exits.append({"share": remaining, "usd": 0.0, "reason": "no route to sell: counted as lost", "at": time.time()})
                set_state(con, tid, "CLOSED", exits=json.dumps(exits), closed_at=time.time(), pnl_usd=proceeds - entry_usd - costs)
                ledger(con, "close", {"trade": tid, "why": "no route to sell", "pnl_usd": round(proceeds - entry_usd - costs, 4)})
            else:
                con.execute("update trades set exits=? where id=?", (json.dumps(exits), tid))
                con.commit()
            continue
        value_usd = int(q["outAmount"]) / 1e9 * sol_price
        price = value_usd / (tokens * remaining) if remaining > 0 else 0.0
        pos = lf.Position(mint, entry_price, opened, remaining=remaining, peak=peak,
                          taken=[e["level"] for e in exits if e.get("level")], holders=json.loads(holders_json))
        balances = holder_balances(list(pos.holders)) if pos.holders else None
        sales = lf.step(pos, price, time.time(), plan, balances)
        for share, reason in sales:
            usd = value_usd * share / remaining if remaining else 0.0
            level = next((lvl for lvl, _ in plan.take_profits if reason == f"take profit at {lvl}x"), None)
            exits.append({"share": share, "usd": round(usd, 4), "reason": reason, "at": time.time(), "multiple": round(price / entry_price, 3), "level": level})
            proceeds += usd
            costs += tx_cost_sol * sol_price
            ledger(con, "exit", {"trade": tid, "share": share, "usd": round(usd, 4), "reason": reason})
        if pos.remaining <= 1e-9:
            set_state(con, tid, "CLOSED", exits=json.dumps(exits), proceeds_usd=proceeds, costs_usd=costs, peak=pos.peak,
                      closed_at=time.time(), pnl_usd=proceeds - entry_usd - costs)
            ledger(con, "close", {"trade": tid, "pnl_usd": round(proceeds - entry_usd - costs, 4)})
        elif sales:
            set_state(con, tid, "PARTIAL_EXIT", exits=json.dumps(exits), proceeds_usd=proceeds, costs_usd=costs, peak=pos.peak)
        else:
            con.execute("update trades set exits=?, peak=? where id=?", (json.dumps(exits), pos.peak, tid))
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
    states = dict(con.execute("select state, count(*) from trades group by state").fetchall())
    pnl = [r[0] for r in con.execute("select pnl_usd from trades where state in ('CLOSED','FAILED_SAFE') and pnl_usd is not null").fetchall()]
    closed = [r[0] for r in con.execute("select pnl_usd from trades where state='CLOSED' and pnl_usd is not null").fetchall()]
    return {
        "tokens_screened": len(tokens),
        "rejected": sum(1 for v, _ in tokens if v == "rejected"), "watching": sum(1 for v, _ in tokens if v == "watching"),
        "entered": sum(1 for v, _ in tokens if v == "entered"),
        "top_rejection_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:8]),
        "trades_by_state": states, "trades_closed": len(closed), "trades_open": states.get("OPEN", 0) + states.get("PARTIAL_EXIT", 0),
        "net_pnl_usd": round(sum(pnl), 2), "expectancy_usd": round(sum(pnl) / len(pnl), 3) if pnl else None,
        "win_rate_pct": round(sum(1 for p in closed if p > 0) / len(closed) * 100, 1) if closed else None,
        "best_usd": round(max(closed), 2) if closed else None, "worst_usd": round(min(closed), 2) if closed else None,
        "phase_2_gate": "met" if len(closed) >= 200 and sum(pnl) > 0 else f"not met: needs 200+ closed trades with positive net result ({len(closed)} so far)",
        "ledger": verify(con),
        "not_measured": ["MEV against a real order", "wash trading by one wallet"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("memecoin_shadow.db"))
    parser.add_argument("--minutes", type=float, default=60.0, help="how long to run (0 = until stopped)")
    parser.add_argument("--every", type=float, default=20.0, help="seconds between passes")
    parser.add_argument("--size-usd", type=float, default=20.0, help="simulated position size")
    parser.add_argument("--tx-cost", type=float, default=0.0003, help="SOL per simulated transaction (priority fee + tip)")
    parser.add_argument("--latency", type=float, default=2.0, help="seconds between deciding and landing")
    parser.add_argument("--kill-usd", type=float, default=100.0, help="stop new entries for the day once closed trades lose this much")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    con = db(args.db)
    if args.report or args.verify:
        print(json.dumps(verify(con) if args.verify else report(con), indent=1))
        return
    plan, end = lf.ExitPlan(), time.time() + args.minutes * 60 if args.minutes else float("inf")
    while time.time() < end:
        started = time.time()
        price = sol_usd()
        if price:
            try:
                manage(con, price, plan, args.tx_cost)
            except (KeyError, TypeError, ValueError) as error:  # never let one bad answer stop the exits for good
                print("manage error", type(error).__name__, error)
            for mint in discover():
                try:
                    screen(con, mint, price, args.size_usd, args.tx_cost, args.latency, args.kill_usd)
                except (KeyError, TypeError, ValueError, AttributeError) as error:  # one malformed answer skips one token
                    print("skip", mint, type(error).__name__)
            summary = report(con)
            print(time.strftime("%H:%M:%S"), {k: summary[k] for k in ("tokens_screened", "rejected", "entered", "trades_open", "trades_closed", "net_pnl_usd")},
                  "BREAKER OPEN" if breaker_open() else "", flush=True)
        time.sleep(max(1.0, args.every - (time.time() - started)))
    print(json.dumps(report(con), indent=1))


if __name__ == "__main__":
    main()

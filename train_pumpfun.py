"""Record pump.fun launches from its Solana program, then train and test the pump.fun bot on them.

Three commands:

    python train_pumpfun.py record --minutes 240 --out data/pumpfun     # every launch, every trade
    python train_pumpfun.py train --data data/pumpfun                   # writes loki_v8/pumpfun_record.json
    python train_pumpfun.py paper --minutes 60                          # run the trained bot live, on paper

Recording subscribes to the pump.fun program's logs on a Solana node (``SOLANA_WS_URL``, default the
public endpoint) and keeps every standard SOL launch created while it runs, with every trade on it
and its graduation, so the sample has no survivorship bias: the many launches that die are in it.
Each trade carries the curve's reserves, so a dropped connection loses detail but never the price;
reconnects are logged as gaps.

Training replays those launches through ``loki_v8/pumpfun_bot.py`` with the curve's exact fills, the
fees the program charged, a landing delay and a per-transaction cost; picks the rule on the earliest
launches, checks it on the next ones, and then reports it once on the latest launches it never saw.

Paper mode runs that rule on the live stream (``LivePaper``, the same machine as the browser bot) and
writes what it did to ``--out``. Nothing signs or sends a transaction in any mode.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set, TextIO

from loki_v8 import pumpfun as pf

PUBLIC_WS = "wss://api.mainnet-beta.solana.com"


# --- record --------------------------------------------------------------------------------------
class Recorder:
    def __init__(self, out_dir: Path, rotate_minutes: int = 60) -> None:
        self.out_dir = out_dir
        self.rotate_s = rotate_minutes * 60
        self.tracked: Set[str] = set()
        self.fh: Optional[TextIO] = None
        self.opened = 0.0
        self.counts = {"create": 0, "trade": 0, "complete": 0, "gap": 0}
        out_dir.mkdir(parents=True, exist_ok=True)

    def write(self, row: Dict[str, Any]) -> None:
        now = time.time()
        if self.fh is None or now - self.opened > self.rotate_s:
            if self.fh:
                self.fh.close()
            name = time.strftime("pump-%Y%m%dT%H%M%SZ.jsonl.gz", time.gmtime(now))
            self.fh = gzip.open(self.out_dir / name, "wt", encoding="utf-8")
            self.opened = now
        assert self.fh is not None
        self.fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    def handle(self, slot: int, logs: list, at: float) -> None:
        from loki_v8.pumpfun_bot import rows_from_logs

        for row in rows_from_logs(slot, logs, at):
            if row["k"] == "c":
                self.tracked.add(row["m"])
                self.counts["create"] += 1
                self.write(row)
            elif row["m"] in self.tracked:
                self.counts["trade" if row["k"] == "t" else "complete"] += 1
                self.write(row)

    def gap(self, start: float, end: float) -> None:
        self.counts["gap"] += 1
        self.write({"k": "gap", "from": round(start, 3), "to": round(end, 3)})

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


async def record(out_dir: Path, minutes: float) -> Dict[str, int]:
    rec = Recorder(out_dir)
    try:
        await stream(rec, minutes, lambda: rec.counts)
    finally:
        rec.close()
    return rec.counts


async def stream(rec: Any, minutes: float, status: Any) -> None:
    """Feed every successful pump.fun transaction's logs to ``rec.handle`` until time is up."""
    import websockets  # only live modes need it

    url = os.environ.get("SOLANA_WS_URL", PUBLIC_WS)
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    stop_at = time.time() + minutes * 60 if minutes > 0 else float("inf")
    last_report = time.time()
    dropped_at: Optional[float] = None
    sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
           "params": [{"mentions": [pf.PROGRAM]}, {"commitment": "confirmed"}]}
    while time.time() < stop_at:
        try:
            async with websockets.connect(url, ssl=ctx, max_size=2 ** 24, open_timeout=20, ping_interval=20) as ws:
                await ws.send(json.dumps(sub))
                if dropped_at is not None and hasattr(rec, "gap"):
                    rec.gap(dropped_at, time.time())
                dropped_at = None
                while time.time() < stop_at:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    params = msg.get("params")
                    if not params:
                        continue
                    value = params["result"]["value"]
                    if value.get("err"):
                        continue
                    rec.handle(params["result"]["context"]["slot"], value.get("logs") or [], time.time())
                    if time.time() - last_report > 300:
                        last_report = time.time()
                        print(time.strftime("%H:%M:%S"), json.dumps(status()), flush=True)
        except (OSError, asyncio.TimeoutError, Exception) as error:  # noqa: BLE001 - reconnect on any drop
            print(time.strftime("%H:%M:%S"), "reconnecting after", type(error).__name__, str(error)[:120], flush=True)
            dropped_at = dropped_at or time.time()
            await asyncio.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record launches and trades from the pump.fun program")
    r.add_argument("--minutes", type=float, default=240)
    r.add_argument("--out", type=Path, default=Path("data/pumpfun"))
    pp = sub.add_parser("paper", help="run the trained rule live on paper (nothing is signed or sent)")
    pp.add_argument("--minutes", type=float, default=60)
    pp.add_argument("--record", type=Path, default=Path("loki_v8/pumpfun_record.json"))
    pp.add_argument("--out", type=Path, default=Path("pumpfun_paper.json"))
    t = sub.add_parser("train", help="train and test the bot on recorded launches")
    t.add_argument("--data", type=Path, default=Path("data/pumpfun"))
    t.add_argument("--out", type=Path, default=Path("loki_v8/pumpfun_record.json"))
    args = parser.parse_args()
    if args.cmd == "record":
        counts = asyncio.run(record(args.out, args.minutes))
        print("done", json.dumps(counts))
    elif args.cmd == "paper":
        from loki_v8 import pumpfun_bot
        rec = json.loads(args.record.read_text(encoding="utf-8"))
        if not rec.get("rule"):
            sys.exit("The record has no rule that passed its checks; nothing to run.")
        c = rec["costs"]
        bot = pumpfun_bot.LivePaper(rec["rule"], pumpfun_bot.Costs(c["stake_sol"], c["tx_cost_sol"], c["latency_s"], c["max_slippage"]))
        try:
            asyncio.run(stream(bot, args.minutes, lambda: {k: v for k, v in bot.report().items() if k != "trades"}))
        finally:
            args.out.write_text(json.dumps(bot.report(), indent=1) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in bot.report().items() if k != "trades"}, indent=1))
    else:
        from loki_v8 import pumpfun_bot
        result = pumpfun_bot.train(args.data)
        args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(result.get("summary", {}), indent=1))
        sys.exit(0)


if __name__ == "__main__":
    main()

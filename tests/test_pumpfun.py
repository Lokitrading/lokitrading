"""pump.fun connector and bot: decoding the program's events, curve maths, features, fills, training, API."""
from __future__ import annotations

import base64
import gzip
import json
import struct
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from loki_v8 import pumpfun as pf
from loki_v8 import pumpfun_bot as pb

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pumpfun_events.json").read_text(encoding="utf-8"))


# --- decoding --------------------------------------------------------------------------------------
def test_events_are_read_only_while_pump_fun_executes():
    events = pf.events_from_logs(FIXTURE["logs"])
    assert [e["kind"] for e in events] == [e["kind"] for e in FIXTURE["expected"]]
    assert events == FIXTURE["expected"]
    # the lookalike trade logged by another program and the corrupt record are both skipped
    assert sum(1 for e in events if e["kind"] == "trade") == 4


def test_standard_and_mayhem_launches_are_told_apart():
    creates = [e for e in pf.events_from_logs(FIXTURE["logs"]) if e["kind"] == "create"]
    assert [pf.is_standard(c) for c in creates] == [True, True, False]
    assert [c["mayhem"] for c in creates] == [False, True, False]
    assert creates[0]["vsol"] == pf.START_VSOL and creates[0]["vtok"] == pf.START_VTOKENS


def test_recorded_fill_matches_the_curve_maths():
    first = [e for e in FIXTURE["expected"] if e["kind"] == "trade"][0]
    assert first["vsol"] == pf.START_VSOL + first["sol"]  # the first buy on a fresh curve
    tokens = pf.buy_tokens(pf.START_VSOL, pf.START_VTOKENS, pf.START_RTOKENS, first["sol"], 0)
    assert tokens == pytest.approx(first["tokens"], rel=1e-6)


def test_curve_maths_cases():
    for c in FIXTURE["math"]:
        assert pf.buy_tokens(c["vsol"], c["vtok"], c["rtok"], c["lamports"], c["fee"]) == c["tokens"]
        assert pf.sell_sol(c["vsol"], c["vtok"], c["tokens"], c["fee"]) == c["sell"]
        assert c["sell"] < c["lamports"]  # selling back into the pre-buy reserves always loses
    assert pf.progress(pf.START_RTOKENS) == 0 and pf.progress(0) == 1
    assert pf.market_cap_sol(pf.START_VSOL, pf.START_VTOKENS) == pytest.approx(27.96, abs=0.01)


def test_round_trip_with_our_own_buy_costs_about_two_fees():
    la = pb.Launch("M", "T", "d", 0.0, 1)
    la.rows = [(0.5, 1, 1, 0, "a", 0, pf.START_VSOL, pf.START_VTOKENS, pf.START_RTOKENS, 125),
               (5.0, 1, 1, 0, "b", 0, pf.START_VSOL, pf.START_VTOKENS, pf.START_RTOKENS, 125)]
    p = pb.prepare(la)
    r = pb.exit_returns(p, 0, [pb.Exit(9, -0.99, None, 3)], pb.Costs(size_sol=1.0, tx_cost_sol=0.0))
    assert r[0] == pytest.approx(-0.025, abs=0.002)  # 1.25% in, 1.25% out, our own impact given back


def test_bonding_curve_address_and_account():
    ex = FIXTURE["curve_example"]
    assert pf.bonding_curve_address(ex["mint"]) == ex["bonding_curve"]
    assert pf.is_mint(ex["mint"]) and not pf.is_mint("not-a-mint") and not pf.is_mint("0" * 40)
    creator = pf.b58decode(ex["mint"])
    data = (b"\0" * 8 + struct.pack("<QQQQQ", 700 * 10 ** 12, 46 * 10 ** 9, 420 * 10 ** 12, 16 * 10 ** 9, 10 ** 15)
            + b"\0" + creator + b"\1" + b"\0" * 84)
    st = pf.parse_curve_account(data)
    assert st["complete"] is False and st["mayhem"] is True and st["creator"] == ex["mint"]
    assert st["raised_sol"] == 16 and 0 < st["progress"] < 1


# --- features and fills ----------------------------------------------------------------------------
DEV = "dev"


def _launch(rows, completed_at=None, mayhem=False):
    """Rows: (seconds after launch, buy, lamports, user). Reserves follow the real curve maths."""
    la = pb.Launch("MINT", "TST", DEV, 1000.0, 100, mayhem, completed_at)
    vs, vt, rt = pf.START_VSOL, pf.START_VTOKENS, pf.START_RTOKENS
    for k, (dt, buy, lam, user) in enumerate(rows):
        if buy:
            tok = pf.buy_tokens(vs, vt, rt, lam, 0)
            vs, vt, rt = vs + lam, vt - tok, rt - tok
        else:
            tok = lam  # for sells the third field is a token amount
            out = vs - (vs * vt) // (vt + tok)
            vs, vt, rt, lam = vs - out, vt + tok, rt + tok, out
        la.rows.append((1000.0 + dt, 100 + k, int(buy), lam, user, tok, vs, vt, rt, 125))
    return la


def test_tracker_features_use_only_the_past():
    la = _launch([(0.5, 1, 10 ** 9, DEV), (2, 1, 2 * 10 ** 9, "a"), (3, 1, 10 ** 9, "b"), (40, 1, 10 ** 8, "c")])
    p = pb.prepare(la)
    f = dict(zip(pb.FEATURES, p.feats[2]))
    assert f["buyers30"] == 2 and f["flow30"] == pytest.approx(4.0) and f["dev_sold"] == 0
    assert f["bundle"] > 0  # the dev's buy landed in the launch block
    late = dict(zip(pb.FEATURES, p.feats[3]))
    assert late["buyers30"] == 1 and late["age"] == pytest.approx(40)  # the 30-second window moved on


def test_take_profit_and_stop_loss_with_latency():
    rise = _launch([(1, 1, 10 ** 9, "a")] + [(2 + i, 1, 10 ** 9, f"u{i}") for i in range(20)])
    p = pb.prepare(rise)
    ex = [pb.Exit(tp=0.3, sl=-0.3, trail=None, hold_s=600)]
    r = pb.exit_returns(p, 0, ex, pb.Costs(latency_s=1.5))
    assert r[0] > 0.3 - 0.05  # take profit landed a little after the trigger
    fall = _launch([(1, 1, 5 * 10 ** 9, "a"), (3, 0, 200 * 10 ** 12, "a"), (5, 0, 200 * 10 ** 12, "b")])
    p = pb.prepare(fall)
    r = pb.exit_returns(p, 0, ex, pb.Costs(latency_s=1.5))
    assert r[0] < -0.3


def test_buy_fails_when_price_runs_past_the_slippage_cap():
    jump = _launch([(1, 1, 10 ** 8, "a"), (1.2, 1, 20 * 10 ** 9, "whale")])
    p = pb.prepare(jump)
    r = pb.exit_returns(p, 0, [pb.Exit(1.0, -0.5, None, 60)], pb.Costs())
    assert r[0] == pytest.approx(-pb.TX_COST_SOL / pb.SIZE_SOL)  # only the fee is lost


def test_creator_sale_and_graduation_force_exits():
    la = _launch([(1, 1, 10 ** 9, DEV), (2, 1, 10 ** 9, "a"), (10, 0, 10 ** 12, DEV), (20, 1, 10 ** 9, "b")])
    p = pb.prepare(la)
    r_dev = pb.exit_returns(p, 1, [pb.Exit(5.0, -0.9, None, 600)], pb.Costs())
    grad = _launch([(1, 1, 10 ** 9, "a"), (2, 1, 10 ** 9, "b"), (30, 1, 10 ** 9, "c")], completed_at=1031.0)
    g = pb.exit_returns(pb.prepare(grad), 0, [pb.Exit(5.0, -0.9, None, 600)], pb.Costs())
    assert np.isfinite(r_dev[0]) and np.isfinite(g[0])
    assert g[0] < pb.exit_returns(pb.prepare(grad), 0, [pb.Exit(5.0, -0.9, None, 600)], pb.Costs())[0] + 1e-12


def test_silences_count_as_gaps_and_only_early_overlaps_are_dropped():
    early = pb.Launch("A", "A", "d", 1000.0, 1)
    later = pb.Launch("B", "B", "d", 1500.0, 2)
    kept = pb.usable([early, later], [(1050.0, 1056.0)], end=5000.0)
    assert [la.mint for la in kept] == ["B"]  # A's first two minutes touch the gap


def test_live_decide_matches_the_backtest_mask():
    rc = FIXTURE["rule_cases"]
    assert [pb.decide(f, rc["rule"]) for f in rc["features"]] == rc["expected"]
    e = pb.Entry(60, 0.05, 0.3, 8, 1, 0.1, 0.3, True, "exclude")
    feats = np.array([[f.get(n, 0.0) for n in pb.FEATURES] for f in rc["features"]], dtype=float)
    assert list(pb.entry_mask(feats, e)) == rc["expected"]


# --- training on a synthetic recording -------------------------------------------------------------
def _write_recording(folder: Path, n: int = 120) -> None:
    rng = np.random.default_rng(3)
    rows = []
    t0 = 1_800_000_000.0
    for i in range(n):
        mint, at = f"M{i}", t0 + i * 20
        rows.append({"k": "c", "slot": i * 50, "at": at, "ts": int(at), "m": mint, "sym": f"T{i}", "name": "t", "dev": f"d{i}", "may": int(i % 2)})
        vs, vt, rt = pf.START_VSOL, pf.START_VTOKENS, pf.START_RTOKENS
        hot = i % 3 == 0
        for k in range(40):
            buy = hot or rng.random() < 0.45
            lam = int(rng.uniform(0.2, 1.5) * 1e9)
            if buy:
                tok = pf.buy_tokens(vs, vt, rt, lam, 0)
                vs, vt, rt = vs + lam, vt - tok, rt - tok
            else:
                tok = min(int(rng.uniform(1, 20) * 1e12), pf.START_RTOKENS - rt)
                if tok <= 0:
                    continue
                lam = vs - (vs * vt) // (vt + tok)
                vs, vt, rt = vs - lam, vt + tok, rt + tok
            rows.append({"k": "t", "slot": i * 50 + k, "at": at + 1 + k * 3, "ts": int(at), "m": mint, "b": int(buy), "sol": lam,
                         "tok": tok, "u": f"u{rng.integers(0, 30)}", "vs": vs, "vt": vt, "rt": rt, "fee": 125})
    rows.append({"k": "t", "slot": 10 ** 7, "at": t0 + n * 20 + 2000, "ts": 0, "m": "M0", "b": 1, "sol": 1, "tok": 1, "u": "z",
                 "vs": pf.START_VSOL, "vt": pf.START_VTOKENS, "rt": pf.START_RTOKENS, "fee": 125})
    with gzip.open(folder / "pump-20270115T000000Z.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_train_reports_fit_check_and_untouched_test(tmp_path, monkeypatch):
    _write_recording(tmp_path)
    monkeypatch.setattr(pb, "SILENCE_S", 1e9)  # the synthetic recording ends with one long, deliberate silence
    monkeypatch.setattr(pb, "ENTRY_GRID", {**pb.ENTRY_GRID, "age_max": [60.0, 600.0], "buyers_min": [3.0], "flow_min": [0.0, 3.0],
                                           "bundle_max": [1.0], "top_max": [1.0]})
    res = pb.train(tmp_path)
    assert res["recorded"]["used"] == 120
    sp = res["split"]
    assert sp["fit"]["launches"] == 60 and sp["check"]["launches"] == 30 and sp["test"]["launches"] == 30
    assert sp["fit"]["from"] < sp["check"]["from"] < sp["test"]["from"]  # time order, no shuffling
    assert res["verdict"] in ("passed", "did not pass", "no rule passed")
    assert "buy_every_launch_at_5s_hold_60s" in res["baselines_test"]
    if res["verdict"] != "no rule passed":
        assert set(res["rule"]) >= {"age_max", "take_profit", "stop_loss", "max_hold_s", "mayhem"}
        assert res["test"]["trades"] >= 0 and "test_stress" in res


def test_live_paper_enters_and_exits_on_the_stream():
    rule = {"age_max": 600, "prog_lo": 0.0, "prog_hi": 1.0, "buyers_min": 1, "flow_min": 0, "bundle_max": 1, "top_max": 1,
            "dev_must_hold": False, "mayhem": "any", "take_profit": 0.2, "stop_loss": -0.5, "trail": None, "trail_arm": 0.2, "max_hold_s": 600}
    bot = pb.LivePaper(rule, pb.Costs(latency_s=1.0))
    create = next(e for e in FIXTURE["logs"] if e.startswith("Program data: ") and base64.b64decode(e[14:])[:8] == pf.CREATE)
    raw = base64.b64decode(create[14:])
    wrap = lambda line: [f"Program {pf.PROGRAM} invoke [1]", line, f"Program {pf.PROGRAM} success"]  # noqa: E731
    bot.handle(1, wrap(create), 100.0)
    mint = pf.parse_create(raw)["mint"]
    assert bot.seen == 1

    def trade(at: float, lam: int) -> None:
        st = bot.launches[mint]["state"] or {"vs": pf.START_VSOL, "vt": pf.START_VTOKENS, "rt": pf.START_RTOKENS}
        tok = pf.buy_tokens(st["vs"], st["vt"], st["rt"], lam, 0)
        body = (pf.TRADE + pf.b58decode(mint) + struct.pack("<QQ", lam, tok) + b"\1" + b"\2" * 32
                + struct.pack("<qQQQQ", int(at), st["vs"] + lam, st["vt"] - tok, 0, st["rt"] - tok)
                + b"\0" * 32 + struct.pack("<QQ", 95, 0) + b"\0" * 32 + struct.pack("<QQ", 30, 0))
        bot.handle(2, wrap("Program data: " + base64.b64encode(body).decode()), at)

    trade(101.0, 10 ** 9)
    assert mint in bot.positions and bot.positions[mint]["status"] == "pending"
    for k in range(20):
        trade(102.0 + k, 10 ** 9)
    trade(130.0, 10 ** 8)
    assert bot.closed and bot.closed[0]["reason"] == "take profit" and bot.closed[0]["return"] > 0.15


# --- API -------------------------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch, tmp_path):
    from loki.api import pumpfun_api
    from loki.api.server import app

    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps({"verdict": "did not pass", "rule": {"age_max": 60}, "costs": {"stake_sol": 0.2}}), encoding="utf-8")
    monkeypatch.setattr(pumpfun_api, "RECORD_PATH", rec)
    pumpfun_api._cache.clear()
    return TestClient(app), pumpfun_api


def test_record_endpoint_serves_rule_and_event_layout(client):
    c, _ = client
    body = c.get("/api/fusion/ai/pumpfun").json()
    assert body["program"] == pf.PROGRAM and body["events"]["trade"] == pf.TRADE.hex()
    assert body["rule"] == {"age_max": 60} and body["curve"]["start_vsol"] == pf.START_VSOL


def test_curve_endpoint_reads_and_validates(client, monkeypatch):
    c, api = client
    ex = FIXTURE["curve_example"]
    data = b"\0" * 8 + struct.pack("<QQQQQ", 700 * 10 ** 12, 46 * 10 ** 9, 420 * 10 ** 12, 16 * 10 ** 9, 10 ** 15) + b"\0" + b"\3" * 32 + b"\0" * 85
    calls = []

    def fake_rpc(method, params):
        calls.append(params[0])
        return {"value": {"owner": pf.PROGRAM, "data": [base64.b64encode(data).decode(), "base64"]}}

    monkeypatch.setattr(api, "_rpc", fake_rpc)
    body = c.get("/api/fusion/ai/pumpfun/curve", params={"mint": ex["mint"]}).json()
    assert calls == [ex["bonding_curve"]] and body["raised_sol"] == 16 and body["standard_curve"] is True
    assert c.get("/api/fusion/ai/pumpfun/curve", params={"mint": "O" * 40}).status_code == 422
    monkeypatch.setattr(api, "_rpc", lambda m, p: {"value": {"owner": "11111111111111111111111111111111", "data": ["", "base64"]}})
    api._cache.clear()
    assert c.get("/api/fusion/ai/pumpfun/curve", params={"mint": ex["mint"]}).status_code == 404


def test_paper_parity_fixture_still_matches_live_paper():
    pp = FIXTURE["paper_parity"]
    c = pp["costs"]
    bot = pb.LivePaper(pp["rule"], pb.Costs(c["stake_sol"], c["tx_cost_sol"], c["latency_s"], c["max_slippage"]))
    for row in pp["rows"]:
        bot.handle_row(row)
    assert bot.failed == pp["failed"] and sorted(bot.positions) == pp["open"]
    assert [(t["symbol"], t["reason"], t["return"]) for t in bot.closed] == [(t["symbol"], t["reason"], t["return"]) for t in pp["closed"]]


class _FakeSocket:
    """Stands in for a Solana node: one transaction with the fixture's logs, then quiet."""

    def __init__(self, logs):
        self.sent = []
        self.queue = [json.dumps({"jsonrpc": "2.0", "result": 7, "id": 1}),
                      json.dumps({"method": "logsNotification", "params": {"result": {"context": {"slot": 42}, "value": {"err": None, "logs": logs}}}}),
                      json.dumps({"method": "logsNotification", "params": {"result": {"context": {"slot": 43}, "value": {"err": {"x": 1}, "logs": logs}}}})]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        import asyncio

        if self.queue:
            return self.queue.pop(0)
        await asyncio.sleep(10)


def test_stream_relays_decoded_rows(client, monkeypatch):
    import sys
    import types

    c, _ = client
    sock = _FakeSocket(FIXTURE["logs"])
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **k: sock))
    rows = [json.loads(line) for line in c.get("/api/fusion/ai/pumpfun/stream", params={"seconds": 5}).text.splitlines() if line]
    kinds = [r["k"] for r in rows]
    assert kinds[0] == "open" and kinds[-1] == "end" and "tick" in kinds
    assert sock.sent[0]["method"] == "logsSubscribe" and sock.sent[0]["params"][0] == {"mentions": [pf.PROGRAM]}
    creates = [r for r in rows if r["k"] == "c"]
    trades = [r for r in rows if r["k"] == "t"]
    assert len(creates) == 2 and len(trades) == 4  # standard launches only; the failed transaction is dropped
    assert {"m", "b", "sol", "tok", "u", "vs", "vt", "rt", "fee", "slot", "at"} <= set(trades[0])


def test_stream_reports_a_dead_node_instead_of_hanging(client, monkeypatch):
    import sys
    import types

    c, _ = client

    def refuse(*a, **k):
        raise OSError("refused")

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=refuse))
    rows = [json.loads(line) for line in c.get("/api/fusion/ai/pumpfun/stream", params={"seconds": 5}).text.splitlines() if line]
    assert rows == [{"k": "error", "detail": "Solana's stream could not be read (OSError)."}]


def test_learned_model_scores_and_gates_like_the_fixture():
    mc = FIXTURE["model_case"]
    feats = np.array([[f[n] for n in pb.FEATURES] for f in mc["features"]], dtype=float)
    assert np.allclose(pb.score(mc["model"], feats), mc["scores"], atol=1e-9)
    rule = {"age_max": 1e9, "prog_lo": 0, "prog_hi": 1, "buyers_min": 0, "flow_min": -1e9, "bundle_max": 1, "top_max": 1,
            "dev_must_hold": False, "mayhem": "any", "model": mc["model"]}
    assert [pb.decide(f, rule) for f in mc["features"]] == [s >= mc["model"]["threshold"] for s in mc["scores"]]

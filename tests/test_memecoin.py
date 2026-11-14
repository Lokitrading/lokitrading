"""Memecoin paper bot: rule signals, no look-ahead, costs, sizing, the record and the endpoint."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from loki_v8 import memecoin as mc

ROOT = Path(__file__).resolve().parents[1]


def series(values, freq="4h"):
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq=freq, tz="UTC"), dtype=float)


def test_breakout_enters_on_a_new_high_and_leaves_on_a_new_low():
    close = series([10] * 10 + [12] + [12.5] * 5 + [9])
    sig = mc.signal(close, {"family": "breakout", "bars": "4h", "entry": 5, "exit": 3})
    assert sig.iloc[9] == 0 and sig.iloc[10] == 1  # 12 breaks the 5-bar high of 10
    assert sig.iloc[15] == 1 and sig.iloc[16] == 0  # 9 breaks the 3-bar low of 12.5


def test_trend_and_momentum_follow_their_definitions():
    up = series(np.linspace(1, 3, 400))
    assert mc.signal(up, {"family": "trend", "bars": "4h", "fast": 30, "slow": 180}).iloc[-1] == 1
    assert mc.signal(up, {"family": "trend", "bars": "4h", "fast": 30, "slow": 180}).iloc[:180].sum() == 0  # not before the slow average is ready
    down = series(np.linspace(3, 1, 400))
    assert mc.signal(down, {"family": "momentum", "bars": "4h", "lookback": 42}).iloc[-1] == 0


def test_a_decision_only_earns_from_the_next_bar_and_pays_costs():
    # flat, then one +50% bar: a rule that enters ON that bar's close must not earn it
    close = series([1.0] * 300 + [1.5] + [1.5] * 5)
    rule = {"family": "breakout", "bars": "4h", "entry": 60, "exit": 30}
    r = mc.book_returns({"X": close}, rule)
    w = mc.weights({"X": close}, rule)
    jump = close.index[300]
    assert w.loc[jump, "X"] > 0  # it buys at that close
    assert r.loc[jump] == 0.0  # but the +50% happened before it held anything
    assert r.loc[close.index[301]] == pytest.approx(-mc.COST * w.loc[jump, "X"])  # it pays to get in


def test_wild_coins_get_smaller_stakes_and_the_book_never_borrows():
    calm = series(100 * np.cumprod(1 + np.random.default_rng(1).normal(0.001, 0.01, 800)))
    wild = series(100 * np.cumprod(1 + np.random.default_rng(2).normal(0.001, 0.08, 800)))
    rule = {"family": "momentum", "bars": "4h", "lookback": 1}
    w = mc.weights({"CALM": calm, "WILD": wild}, rule)
    assert (w.sum(axis=1) <= 1.0 + 1e-9).all() and bool((w >= 0).all().all())
    assert w["WILD"][w["WILD"] > 0].max() < w["CALM"][w["CALM"] > 0].max()


def test_the_record_is_honest_about_its_result():
    rec = json.loads((ROOT / "loki_v8" / "memecoin_record.json").read_text())
    assert rec["choose_until"] < rec["test_from"] and rec["chosen"]["label"] == mc.pick({r["label"]: r["choose"]["sharpe"] for r in rec["all_rules"]})
    t = rec["chosen"]["test"]
    expected = t["sharpe"] > 0.5 and t["cagr_pct"] > 0 and t["max_dd_pct"] > rec["hold"]["test"]["max_dd_pct"]
    assert rec["passed"] is expected
    assert set(rec["coins"]) == set(mc.COINS) and any("collapsed" in c for c in rec["caveats"])


def test_the_endpoint_is_paper_only_and_reads_public_prices(monkeypatch):
    from loki.api import memecoin_api

    def fake(symbol, interval, limit):
        rng = np.random.default_rng(len(symbol))
        px = 1 * np.cumprod(1 + rng.normal(0.002, 0.03, limit))
        return tuple({"time": 1_700_000_000 + i * 14400, "close": float(p)} for i, p in enumerate(px))
    monkeypatch.setattr(memecoin_api, "get_binance_spot_history", fake)
    memecoin_api._cache.clear()
    data = memecoin_api.plan()
    assert data["paper_only"] is True and {h["coin"] for h in data["holdings"]} == set(mc.COINS)
    assert 0 <= data["in_market_pct"] <= 100 and data["record"]["rules_tested"] == len(mc.GRID)


def test_the_endpoint_answers_paper_only_and_is_rate_limited(monkeypatch):
    from fastapi.testclient import TestClient
    from loki.api import memecoin_api
    from loki.api.rate_limit import rule_for
    from loki.api.server import app

    def fake(symbol, interval, limit):
        px = np.cumprod(1 + np.random.default_rng(len(symbol)).normal(0.0, 0.03, limit))
        return tuple({"time": 1_700_000_000 + i * 14400, "close": float(p)} for i, p in enumerate(px))
    monkeypatch.setattr(memecoin_api, "get_binance_spot_history", fake)
    memecoin_api._cache.clear()
    res = TestClient(app).get("/api/fusion/ai/memecoin")
    assert res.status_code == 200 and res.json()["paper_only"] is True and res.headers["cache-control"] == "private, no-store"
    assert rule_for("GET", "/api/fusion/ai/memecoin")


def test_launch_radar_screens_new_tokens_and_explains_each_verdict(monkeypatch):
    from loki.api import memecoin_api

    good = {"mint": "GOOD", "mintAuthority": None, "freezeAuthority": None, "risks": [], "graphInsidersDetected": 0,
            "markets": [{"pubkey": "CURVE", "marketType": "pump_fun", "lp": {}}], "knownAccounts": {"CURVE": {"type": "AMM"}},
            "topHolders": [{"owner": "CURVE", "pct": 80}] + [{"owner": f"W{i}", "pct": 1} for i in range(10)], "totalMarketLiquidity": 9000,
            "launchpad": {"name": "Pump.Fun"}}
    feeds = {
        "https://api.rugcheck.xyz/v1/stats/new_tokens": [{"mint": "GOOD", "symbol": "GD"}, {"mint": "BAD", "symbol": "BD"}, {"mint": "NEW", "symbol": "NW"}],
        "https://api.rugcheck.xyz/v1/tokens/GOOD/report": good,
        "https://api.rugcheck.xyz/v1/tokens/BAD/report": {**good, "mint": "BAD", "mintAuthority": "X"},
        "https://api.dexscreener.com/tokens/v1/solana/GOOD,BAD,NEW": [
            {"baseToken": {"address": "GOOD"}, "volume": {"m5": 30000}, "txns": {"m5": {"buys": 60, "sells": 20}}, "liquidity": None}],
    }
    monkeypatch.setattr(memecoin_api, "_get", lambda url: feeds.get(url))
    rows = {r["symbol"]: r for r in memecoin_api.radar()["tokens"]}
    assert rows["GD"]["verdict"].startswith("would enter") and rows["GD"]["vol_to_liq"] == 3.33
    assert rows["BD"]["verdict"] == "rejected" and "mint authority" in rows["BD"]["reasons"][0]
    assert rows["NW"]["verdict"] == "no report yet"

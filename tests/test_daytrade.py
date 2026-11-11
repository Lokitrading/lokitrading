"""Day Trader: intraday noise-band momentum, flat every night; live plan mirrors the tested rules."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from loki.api import ai_paper, loki_brain
from loki.api.rate_limit import rule_for
from loki.api.server import app
from loki_v8 import daytrade

ROOT = Path(__file__).resolve().parents[1]


def _session(day: str, path: np.ndarray, volume: float = 1000.0) -> pd.DataFrame:
    """78 five-minute bars for one New York session from a close path (open = previous close)."""
    idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min", tz="America/New_York")
    opens = np.concatenate([[path[0]], path[:-1]])
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, path), "low": np.minimum(opens, path),
                         "close": path, "volume": volume}, index=idx)


def _history(days: int = 20, seed: int = 3, last=None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames, price = [], 100.0
    dates = pd.bdate_range("2026-03-02", periods=days)
    for i, d in enumerate(dates):
        steps = rng.normal(0, 0.0006, 78)
        path = price * np.cumprod(1 + steps)
        if last is not None and i == days - 1:
            path = last(price)
        frames.append(_session(str(d.date()), path))
        price = float(path[-1])
    return pd.concat(frames)


def test_quiet_days_do_nothing_and_a_breakout_is_bought_then_closed():
    quiet = daytrade.backtest(_history(20), cost=1e-4)
    assert len(quiet) == 20 - daytrade.LOOKBACK
    up = _history(20, last=lambda p: p * (1 + np.linspace(0.0005, 0.03, 78)))  # a steady 3% trend day
    r = daytrade.backtest(up, cost=1e-4)
    assert r.iloc[-1] > 0.01  # rode most of the move
    days = daytrade.sessions(up)
    noise = daytrade.noise_profile([daytrade.day_moves(g) for _, g in days[:-1]])
    out = daytrade.replay_day(days[-1][1], noise, float(days[-2][1]["close"].iloc[-1]), 1e-4)
    assert out["position"] == 0 and out["decisions"][0]["to"] == 1 and out["decisions"][-1]["why"] == "session close"
    down = _history(20, last=lambda p: p * (1 - np.linspace(0.0005, 0.03, 78)))
    assert daytrade.backtest(down, cost=1e-4).iloc[-1] > 0.01  # shorts the mirror image
    assert daytrade.backtest(down, cost=1e-4, sides=(1,)).iloc[-1] == 0.0  # long-only stays out


def test_costs_reduce_returns_and_the_future_never_leaks_back():
    bars = _history(24, seed=5, last=lambda p: p * (1 + np.linspace(0.0005, 0.03, 78)))
    assert daytrade.backtest(bars, cost=5e-4).sum() < daytrade.backtest(bars, cost=0.0).sum()
    full = daytrade.backtest(bars, cost=1e-4)
    cut = bars[bars.index < bars.index[-1].normalize()]  # drop the last session
    assert daytrade.backtest(cut, cost=1e-4).equals(full.iloc[:-1])


def test_live_state_is_flat_when_closed_and_mirrors_the_rules_mid_session():
    bars = _history(20, last=lambda p: p * (1 + np.linspace(0.0005, 0.03, 78)))
    last_day = bars.index[-1].normalize()
    closed = daytrade.live_state(bars, "QQQ", now=(last_day + pd.Timedelta(hours=20)).tz_convert("UTC"))
    assert closed["status"] == "closed" and closed["position"] == 0
    midday = daytrade.live_state(bars, "QQQ", now=(last_day + pd.Timedelta(hours=13)).tz_convert("UTC"))
    assert midday["status"] == "open" and midday["position"] == 1 and midday["lower"] < midday["upper"] < midday["last"]
    near_bell = daytrade.live_state(bars, "QQQ", now=(last_day + pd.Timedelta(hours=15, minutes=56)).tz_convert("UTC"))
    assert near_bell["position"] == 0  # nothing is carried into the close
    assert daytrade.live_state(_history(5), "QQQ")["status"] == "no_data"


def test_shipped_record_is_honest_about_what_failed():
    rec = json.loads((ROOT / "loki_v8" / "daytrade_record.json").read_text(encoding="utf-8"))
    assert rec["verified"] and rec["windows"]["select"][1] < rec["windows"]["test"][0]
    chosen = {c["symbol"] for c in rec["candidates"] if c["chosen"]}
    assert chosen == set(rec["symbols"]) and all(c["select"]["sharpe"] > 0.3 for c in rec["candidates"] if c["chosen"])
    assert len(rec["candidates"]) == 12 and sum(1 for c in rec["candidates"] if c["test"]["cagr_pct"] < 0) >= 5
    assert rec["book"]["test"]["cagr_pct"] < rec["book"]["select"]["cagr_pct"]  # weaker on unseen data, and it says so
    assert any(y["return_pct"] < 0 for y in rec["calendar_years"]) and rec["rejected_rules"] and rec["caveats"]


def test_daytrade_endpoint_serves_and_caches_the_plan(monkeypatch):
    calls = []

    def fake_plan():
        calls.append(1)
        return {"market_open": False, "markets": [{"symbol": "QQQ", "status": "closed", "position": 0}], "weight_per_market": 0.2}

    monkeypatch.setattr(ai_paper, "_daytrade_plan", fake_plan)
    ai_paper._DAYTRADE_CACHE.clear()
    client = TestClient(app)
    first = client.get("/api/fusion/ai/daytrade")
    assert first.status_code == 200 and first.headers["cache-control"].startswith("private") or "no-store" in first.headers["cache-control"]
    assert client.get("/api/fusion/ai/daytrade").json()["markets"][0]["symbol"] == "QQQ" and len(calls) == 1
    assert rule_for("GET", "/api/fusion/ai/daytrade")
    ai_paper._DAYTRADE_CACHE.clear()


def test_live_plan_uses_the_tested_engine(monkeypatch):
    bars = _history(20)

    class Provider:
        def fetch_history(self, symbol, period, interval):
            assert (period, interval) == ("60d", "5m")
            return bars

    import loki_v8.data as data
    monkeypatch.setattr(data, "get_provider", lambda: Provider())
    plan = ai_paper._daytrade_plan()
    assert [m["symbol"] for m in plan["markets"]] == plan["record"]["symbols"]
    assert plan["weight_per_market"] == pytest.approx(1 / len(plan["markets"]), abs=1e-4)
    assert all(m["position"] == 0 for m in plan["markets"]) and plan["market_open"] is False  # synthetic bars are in the past


def test_day_trader_is_the_first_panel_and_loki_explains_it():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="day-trader"' in html and html.index('id="day-trader"') < html.index('id="portfolio-ai"')
    assert html.index("day_trader.js") < html.index("portfolio_ai.js")
    answer = loki_brain.answer("is the bot any good at day trading?")
    assert answer["intent"] == "daytrade" and "never saw" in answer["text"] and "lost money on" in answer["text"]

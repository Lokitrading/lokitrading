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


def test_learning_is_kept_only_where_it_earned_it():
    rec = json.loads((ROOT / "loki_v8" / "daytrade_record.json").read_text(encoding="utf-8"))
    learn, control = rec["learning"], rec["learning"]["shuffle_control"]
    assert learn["kept"] and set(learn["models"]) == set(rec["symbols"])
    assert learn["select_window_sharpe"]["filtered"] > learn["select_window_sharpe"]["plain"]
    assert min(control["real_beat_shuffled_select"], control["real_beat_shuffled_test"]) >= 0.9 * control["runs"]  # not luck
    assert rec["book"]["test"]["sharpe"] > rec["book_plain"]["test"]["sharpe"]
    assert rec["book"]["test"]["max_dd_pct"] > rec["book_plain"]["test"]["max_dd_pct"]  # a smaller worst drop
    fut = rec["futures"]
    assert fut["symbols"] == ["MES", "MNQ"] and not fut["learning"]["kept"] and fut["learning"]["models"] == {}
    assert {c["symbol"] for c in fut["candidates"] if not c["chosen"]} == {"M2K", "MYM"}  # the losers are left out
    assert fut["book"]["test"]["cagr_pct"] > 0 and "stand-in" in fut["note"]


def test_mistake_filter_only_uses_the_past_and_can_sit_a_day_out():
    idx = pd.bdate_range("2016-01-04", "2021-12-31")
    rng = np.random.default_rng(1)
    gap = rng.normal(0, 1, len(idx))
    # Days that open with a big gap lose; the rest win: a pattern the filter should learn.
    ret = np.where(np.abs(gap) > 1.0, -0.004, 0.003) + rng.normal(0, 0.0005, len(idx))
    first = [daytrade.features(g / 100, 0.004, 0.0, d.dayofweek, 1, 12) for g, d in zip(gap, idx)]
    frame = pd.DataFrame({"return": ret, "turns": 2, "first": first, "noise": 0.004}, index=idx)
    kept, model = daytrade.learn_from_mistakes(frame, first_year=2018)
    early, late = kept[kept.index < "2018"], kept[kept.index >= "2018"]
    assert early.equals(frame["return"][frame.index < "2018"])  # nothing is filtered before it has lessons
    assert (late == 0).sum() > 100 and late.sum() > frame["return"][frame.index >= "2018"].sum()  # it skipped losers
    assert model["lessons"] == len(idx)
    assert daytrade.rate(model, daytrade.features(0.03, 0.004, 0.0, 1, 1, 12)) < daytrade.rate(model, daytrade.features(0.0, 0.004, 0.0, 1, 1, 12))
    # Live: a gate that rates everything poorly keeps the day flat and says why.
    bars = _history(20, last=lambda p: p * (1 + np.linspace(0.0005, 0.03, 78)))
    days = daytrade.sessions(bars)
    noise = daytrade.noise_profile([daytrade.day_moves(g) for _, g in days[:-1]])
    never = {"model": {"w": [-9.0] + [0.0] * len(daytrade.FEATURES), "mu": [0.0] * len(daytrade.FEATURES), "sd": [1.0] * len(daytrade.FEATURES)}, "threshold": 0.4}
    out = daytrade.replay_day(days[-1][1], noise, float(days[-2][1]["close"].iloc[-1]), 1e-4, gate=never)
    assert out["skipped"] and out["return"] == 0.0 and out["position"] == 0 and "past mistakes" in out["decisions"][0]["why"]


def test_futures_mode_reads_the_fund_and_sizes_in_contracts(monkeypatch):
    bars = _history(20)
    asked = []

    class Provider:
        def fetch_history(self, symbol, period, interval):
            asked.append(symbol)
            return bars

        def get_quotes(self, symbols):
            return {"NQ=F": 25000.0, "ES=F": 7000.0}

    import loki_v8.data as data
    monkeypatch.setattr(data, "get_provider", lambda: Provider())
    plan = ai_paper._daytrade_plan("futures")
    assert plan["mode"] == "futures" and [m["symbol"] for m in plan["markets"]] == ["MES", "MNQ"] and set(asked) == {"SPY", "QQQ"}
    by = {m["symbol"]: m for m in plan["markets"]}
    assert by["MNQ"]["contract_notional"] == 50000.0 and by["MES"]["contract_notional"] == 35000.0
    assert daytrade.cost_for("MNQ") < daytrade.cost_for("QQQ") < daytrade.cost_for("TSLA")
    assert TestClient(app).get("/api/fusion/ai/daytrade?mode=options").status_code == 422


def test_daytrade_endpoint_serves_and_caches_the_plan(monkeypatch):
    calls = []

    def fake_plan():
        calls.append(1)
        return {"market_open": False, "markets": [{"symbol": "QQQ", "status": "closed", "position": 0}], "weight_per_market": 0.2}

    monkeypatch.setattr(ai_paper, "_daytrade_plan", lambda mode="stocks": fake_plan())
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
    assert 'data-dt-mode="futures"' in html
    answer = loki_brain.answer("is the bot any good at day trading?")
    assert answer["intent"] == "daytrade" and "never saw" in answer["text"] and "lost money on" in answer["text"]
    assert "learns from its mistakes" in answer["text"] and "Futures mode" in answer["text"] and "leveraged" in answer["text"]


def test_forward_record_counts_only_completed_sessions_after_the_freeze():
    bars = _history(22, last=lambda p: p * (1 + np.linspace(0.0005, 0.03, 78)))
    days = [str(d.date()) for d, _ in daytrade.sessions(bars)]
    last_open = (bars.index[-1].normalize() + pd.Timedelta(hours=13)).tz_convert("UTC")   # mid-session on the last day
    fwd = daytrade.forward_record(bars, "QQQ", since=days[-4], now=last_open)
    assert list(fwd) == days[-3:-1]  # after the freeze, and today's unfinished session is left out
    after_close = (bars.index[-1].normalize() + pd.Timedelta(hours=17)).tz_convert("UTC")
    full = daytrade.forward_record(bars, "QQQ", since=days[-4], now=after_close)
    assert list(full) == days[-3:] and full[days[-1]] > 0.01
    book = daytrade.summarize_forward({"QQQ": full, "TSLA": {days[-1]: -0.02}}, 2)
    assert book["days"] == 3 and book["losing_days"] + book["winning_days"] <= 3 and book["daily"][-1]["day"] == days[-1]
    assert book["daily"][-1]["return_pct"] == pytest.approx((full[days[-1]] - 0.02) / 2 * 100, abs=1e-3)
    assert daytrade.summarize_forward({}, 5) == {"days": 0}

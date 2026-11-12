"""Day Trader: intraday noise-band momentum, flat every night; live plan mirrors the tested rules."""

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import train_daytrade
from loki.api import ai_paper, loki_brain
from loki.api.rate_limit import rule_for
from loki.api.server import app
from loki_v8 import daytrade

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_day_trader_caches():
    """Each test reads its own prices: nothing is carried over from another test's plan."""
    for cache in (ai_paper._DAYTRADE_CACHE, ai_paper._DAYTRADE_BARS, ai_paper._DAYTRADE_FORWARD):
        cache.clear()
    yield
    for cache in (ai_paper._DAYTRADE_CACHE, ai_paper._DAYTRADE_BARS, ai_paper._DAYTRADE_FORWARD):
        cache.clear()


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
    assert rec["verified"] and rec["windows"]["select"][1] < rec["windows"]["test"][0] and rec["anchor"] == "twap"
    chosen = [c for c in rec["candidates"] if c["chosen"]]
    assert {c["symbol"] for c in chosen} == set(rec["symbols"])
    # A market is in the book only if it passed on the choosing years with instant fills AND with late ones.
    assert all(min(c["select"]["sharpe"], c["select_late"]["sharpe"]) > 0.3 for c in chosen)
    assert not any(min(c["select"]["sharpe"], c["select_late"]["sharpe"]) > 0.3 for c in rec["candidates"] if not c["chosen"])
    everything = rec["all_candidates_book"]
    losers = sum(1 for c in rec["candidates"] if c["test"]["cagr_pct"] < 0)
    assert everything["count"] == len(rec["candidates"]) >= 20 and everything["losing_on_unseen"] == losers > len(rec["candidates"]) / 2
    assert everything["test"]["cagr_pct"] < 0 and str(losers) in rec["caveats"][1]  # most markets lose, and it says so
    assert rec["book"]["test"]["cagr_pct"] < rec["book"]["select"]["cagr_pct"]  # weaker on unseen data, and it says so
    assert any(y["return_pct"] < 0 for y in rec["calendar_years"]) and rec["rejected_rules"] and rec["caveats"]


def test_record_shows_what_slow_fills_cost():
    rec = json.loads((ROOT / "loki_v8" / "daytrade_record.json").read_text(encoding="utf-8"))
    assert rec["late_fill_minutes"] == 5
    for block in (rec, rec["futures"]):
        instant, late = block["book"]["test"], block["book_late"]["test"]
        assert late["cagr_pct"] < instant["cagr_pct"] and late["sharpe"] < instant["sharpe"]  # speed matters, and it says so
        steps = block["fill_speed"]["steps"]
        assert [s["minutes"] for s in steps] == [1, 2, 3] and all(s["test"]["days"] == instant["days"] for s in steps)
        assert steps[0]["test"]["cagr_pct"] < instant["cagr_pct"]  # even one minute late costs something
        assert len(block["calendar_years_late"]) == len(block["calendar_years"])


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
        def fetch_history(self, symbol, period, interval, max_age=60.0):
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

    def fake_plan(mode="stocks"):
        calls.append(mode)
        return {"market_open": False, "markets": [{"symbol": "QQQ", "status": "closed", "position": 0}], "weight_per_market": 0.2}

    clock = [1_000_000_000.0 - (1_000_000_000.0 % 1800) + 400]  # mid-bar, well after a half hour
    monkeypatch.setattr(ai_paper, "_daytrade_plan", fake_plan)
    monkeypatch.setattr(ai_paper.time, "time", lambda: clock[0])
    client = TestClient(app)
    first = client.get("/api/fusion/ai/daytrade")
    assert first.status_code == 200 and first.headers["cache-control"].startswith("private") or "no-store" in first.headers["cache-control"]
    assert first.json()["served_at"]
    clock[0] += 30
    assert client.get("/api/fusion/ai/daytrade").json()["markets"][0]["symbol"] == "QQQ" and len(calls) == 1  # reused
    clock[0] += 30  # older than 45 seconds: rebuilt
    client.get("/api/fusion/ai/daytrade")
    assert len(calls) == 2
    clock[0] = clock[0] - (clock[0] % 300) + 299
    client.get("/api/fusion/ai/daytrade")
    clock[0] += 2  # only two seconds later, but a new 5-minute bar has closed: never reused across it
    client.get("/api/fusion/ai/daytrade")
    assert len(calls) == 4
    assert rule_for("GET", "/api/fusion/ai/daytrade")


def test_prices_are_read_more_often_just_after_a_decision():
    half_hour = 1_000_000_000.0 - (1_000_000_000.0 % 1800)
    assert ai_paper._daytrade_max_age(half_hour + 10) == 15.0 and ai_paper._daytrade_max_age(half_hour + 119) == 15.0
    assert ai_paper._daytrade_max_age(half_hour + 121) == 45.0 and ai_paper._daytrade_max_age(half_hour + 1700) == 45.0


def test_only_todays_bars_are_re_read_between_history_reads(monkeypatch):
    bars = _history(20)
    today = bars[bars.index >= bars.index[-1].normalize()]
    asked = []

    class Provider:
        def fetch_history(self, symbol, period, interval, max_age=60.0):
            asked.append(period)
            return bars if period == "60d" else today.assign(close=today["close"] + 1.0)

    clock = [5000.0]
    monkeypatch.setattr(ai_paper.time, "monotonic", lambda: clock[0])
    first = ai_paper._daytrade_bars(Provider(), "QQQ", 15.0)
    assert asked == ["60d"] and first.equals(bars)
    clock[0] += 5
    assert ai_paper._daytrade_bars(Provider(), "QQQ", 15.0).equals(bars) and asked == ["60d"]  # fresh enough: no request
    clock[0] += 30
    merged = ai_paper._daytrade_bars(Provider(), "QQQ", 15.0)
    assert asked == ["60d", "1d"] and len(merged) == len(bars)  # the small request, spliced onto the kept history
    assert merged["close"].iloc[-1] == bars["close"].iloc[-1] + 1.0 and merged["close"].iloc[0] == bars["close"].iloc[0]
    clock[0] += 700
    ai_paper._daytrade_bars(Provider(), "QQQ", 15.0)
    assert asked == ["60d", "1d", "60d"]  # the history itself is re-read every ten minutes


def test_live_plan_uses_the_tested_engine(monkeypatch):
    bars = _history(20)

    class Provider:
        def fetch_history(self, symbol, period, interval, max_age=60.0):
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


# --- Feed-proof signals, fill speed and reacting quickly -----------------------------
def _trend_day(sign: float = 1.0):
    """A history ending in a steady 3% trend day, split so the last session can be replayed on its own."""
    bars = _history(20, last=lambda p: p * (1 + sign * np.linspace(0.0005, 0.03, 78)))
    days = daytrade.sessions(bars)
    noise = daytrade.noise_profile([daytrade.day_moves(g) for _, g in days[:-1]])
    return bars, days[-1][1], noise, float(days[-2][1]["close"].iloc[-1])


def test_the_average_line_ignores_volume_so_every_feed_gives_the_same_trades():
    bars, session, _, _ = _trend_day()
    other_feed = bars.assign(volume=np.random.default_rng(9).integers(1, 10_000_000, len(bars)).astype(float))
    assert daytrade.backtest(bars, 1e-4).equals(daytrade.backtest(other_feed, 1e-4))
    theirs = daytrade.sessions(other_feed)[-1][1]
    assert np.allclose(daytrade._anchor(session, "twap"), daytrade._anchor(theirs, "twap"))
    assert not np.allclose(daytrade._anchor(session, "vwap"), daytrade._anchor(theirs, "vwap"))  # the published line would have moved


def test_late_fills_earn_less_and_a_short_is_a_fixed_number_of_shares():
    _, session, noise, prev = _trend_day()
    instant = daytrade.replay_day(session, noise, prev, 1e-4)
    late = daytrade.replay_day(session, noise, prev, 1e-4, fill_delay=1)
    assert late["return"] < instant["return"] and late["turns"] == instant["turns"]
    assert pd.Timestamp(late["decisions"][0]["time"]) - pd.Timestamp(instant["decisions"][0]["time"]) == pd.Timedelta(minutes=5)
    _, session, noise, prev = _trend_day(-1.0)
    out = daytrade.replay_day(session, noise, prev, 0.0)
    sold, bought_back = out["decisions"][0], out["decisions"][-1]
    assert len(out["decisions"]) == 2 and sold["to"] == -1 and bought_back["why"] == "session close"
    assert out["return"] == pytest.approx(1 - bought_back["price"] / sold["price"], abs=1e-5)  # entry to exit, nothing compounded in between


def test_a_new_signal_is_reported_at_once_and_rated_before_anything_is_bought():
    _, session, noise, prev = _trend_day()
    so_far = session.iloc[:6]  # 09:30-09:55: the first decision bar has just closed
    live = daytrade.replay_day(so_far, noise, prev, 1e-4, session_complete=False)
    assert live["position"] == 0 and live["pending"] == 1  # decided; fills at the next bar
    never = {"model": {"w": [-9.0] + [0.0] * len(daytrade.FEATURES), "mu": [0.0] * len(daytrade.FEATURES), "sd": [1.0] * len(daytrade.FEATURES)}, "threshold": 0.4}
    held_back = daytrade.replay_day(so_far, noise, prev, 1e-4, session_complete=False, gate=never)
    assert held_back["pending"] is None and held_back["skipped"] and "past mistakes" in held_back["decisions"][0]["why"]


def test_live_state_gives_the_latest_price_the_signal_time_and_goes_flat_when_the_feed_stops():
    bars, session, _, _ = _trend_day()
    day = bars.index[-1].normalize()

    def at(hour, minute, second=0):
        return (day + pd.Timedelta(hours=hour, minutes=minute, seconds=second)).tz_convert("UTC")

    def feed(now):  # what a live feed holds at ``now``: nothing from the future
        return bars[bars.index <= now]

    first_bar = daytrade.live_state(feed(at(9, 32)), "QQQ", now=at(9, 32))
    assert first_bar["status"] == "open" and first_bar["waiting"] and first_bar["position"] == 0
    decided = daytrade.live_state(feed(at(10, 0, 20)), "QQQ", now=at(10, 0, 20))  # 20 seconds after the first decision
    assert decided["position"] == 0 and decided["pending"] == 1 and pd.Timestamp(decided["signal_at"]) == at(10, 0)
    assert decided["last"] == pytest.approx(session["close"].iloc[5], abs=1e-4)  # the bar the rules decided on
    assert decided["now_price"] == pytest.approx(session["close"].iloc[6], abs=1e-4)  # the bar still forming: a paper fill uses this
    filled = daytrade.live_state(feed(at(10, 6)), "QQQ", now=at(10, 6))
    assert filled["position"] == 1 and filled["pending"] is None and pd.Timestamp(filled["signal_at"]) == at(10, 0)
    stalled = daytrade.live_state(feed(at(11, 0)), "QQQ", now=at(11, 50))  # no bar for 45 minutes: halted, or closed early
    assert stalled["status"] == "closed" and stalled["position"] == 0


def test_minute_repricing_matches_the_instant_test_when_price_stands_still(tmp_path):
    bars, _, _, _ = _trend_day()
    (tmp_path / "QQQ.pkl").write_bytes(pickle.dumps(bars))

    def tape(drift):  # 1-minute opens for the seven minutes after each half hour
        rows = {stamp + pd.Timedelta(minutes=m): row["open"] * (1 + drift * m)
                for stamp, row in bars.iterrows() if stamp.minute % 30 == 0 for m in range(7)}
        return pd.DataFrame({"open": pd.Series(rows)}).tz_convert("UTC")

    instant = daytrade.backtest(bars, 1e-4)
    (tmp_path / "QQQ.1min.pkl").write_bytes(pickle.dumps(tape(0.0)))
    still = train_daytrade.late_fills("QQQ", 1e-4, tmp_path)
    assert list(still.columns) == list(train_daytrade.SPEED_MINUTES)
    assert all(np.allclose(still[m], instant, atol=1e-5) for m in still.columns)  # same prices, same result
    (tmp_path / "QQQ.1min.pkl").write_bytes(pickle.dumps(tape(0.0005)))  # price runs away 5 bp a minute after each signal
    chased = train_daytrade.late_fills("QQQ", 1e-4, tmp_path)
    assert chased[1].iloc[-1] < instant.iloc[-1] and chased[3].iloc[-1] < chased[1].iloc[-1]  # the later the fill, the less is left

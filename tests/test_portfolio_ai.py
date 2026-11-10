"""Portfolio AI: verified sleeves combined without leverage; live plan mirrors the replay."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from loki.api import ai_paper
from loki.api.server import app
from loki_v8 import portfolio_ai
from tests.test_live_engine import _tape

PARAMS = {"signal_threshold": 8.0, "sizing_mode": "vol_target", "target_vol_pct": 20.0, "take_profit_atr": 0.0,
          "stop_loss_atr": 6.0, "exit_mode": "trend", "max_hold_bars": 0,
          "factor_weights": {"trend": 0.8, "momentum": 0.05, "volume_flow": 0.05, "squeeze": 0.05, "volatility": 0.05}}


@pytest.fixture(scope="module")
def sleeves():
    return {f"S{i}": portfolio_ai.run_sleeve(_tape(900, seed=i + 3), PARAMS, f"S{i}") for i in range(5)}


def test_sleeve_exposure_matches_positions(sleeves):
    for sleeve in sleeves.values():
        assert sleeve["exposure"].between(0, 1.05).all()
        assert len(sleeve["returns"]) == len(sleeve["exposure"])
        assert sleeve["state"]["as_of"]


def test_book_never_uses_leverage(sleeves):
    book = portfolio_ai.combine(sleeves, 25.0, warmup=260)
    assert (book["scale"] <= portfolio_ai.MAX_SCALE).all()
    assert book["gross"].max() <= 1.0 + 1e-9
    alloc = portfolio_ai.allocation(sleeves, book)
    assert sum(p["weight"] for p in alloc["positions"]) <= 1.0 + 1e-9
    assert alloc["cash"] == pytest.approx(1 - alloc["gross"], abs=1e-3)


def test_diversification_smooths_returns(sleeves):
    book = portfolio_ai.combine(sleeves, 10.0, warmup=260)
    port_vol = book["base_returns"].iloc[300:].std()
    single_vol = np.mean([s["returns"].iloc[300:].std() for s in sleeves.values()])
    assert port_vol < single_vol


def test_target_selection_uses_only_dev_and_validation(sleeves):
    idx = next(iter(sleeves.values()))["returns"].index
    windows = {"development": (str(idx[100].date()), str(idx[400].date())),
               "validation": (str(idx[401].date()), str(idx[600].date()))}
    target, trials = portfolio_ai.select_target_vol(sleeves, windows)
    assert target in (10.0, 15.0, 20.0, 25.0)
    assert all("holdout" not in t for t in trials)


def test_window_stats():
    r = pd.Series([0.01, -0.005] * 100, index=pd.bdate_range("2024-01-01", periods=200))
    s = portfolio_ai.window_stats(r, "2024-01-01", "2030-01-01")
    assert s["cagr_pct"] > 0 and s["sharpe"] > 0 and s["max_dd_pct"] <= 0


def test_portfolio_endpoint_caches_and_reports(monkeypatch):
    calls = []

    def fake_plan():
        calls.append(1)
        return {"target_vol_pct": 10.0, "positions": [{"symbol": "SPY", "weight": 0.1}], "gross": 0.1, "cash": 0.9}

    monkeypatch.setattr(ai_paper, "_portfolio_plan", fake_plan)
    monkeypatch.setattr(ai_paper, "_PORTFOLIO_CACHE", {})
    client = TestClient(app)
    first = client.get("/api/fusion/ai/portfolio").json()
    client.get("/api/fusion/ai/portfolio")
    assert first["positions"][0]["symbol"] == "SPY" and len(calls) == 1
    # A refresh straight after a build reuses it: refresh cannot force rebuilds.
    client.get("/api/fusion/ai/portfolio?refresh=true")
    assert len(calls) == 1
    # Once the plan is older than the refresh floor, refresh rebuilds it.
    stamp, plan = ai_paper._PORTFOLIO_CACHE["plan"]
    ai_paper._PORTFOLIO_CACHE["plan"] = (stamp - ai_paper._PORTFOLIO_MIN_REFRESH_SECONDS - 1, plan)
    client.get("/api/fusion/ai/portfolio?refresh=true")
    assert len(calls) == 2


def test_concurrent_portfolio_requests_share_one_build(monkeypatch):
    import threading
    import time as _time

    calls = []

    def slow_plan():
        calls.append(1)
        _time.sleep(0.3)
        return {"target_vol_pct": 10.0, "positions": [], "gross": 0.0, "cash": 1.0}

    monkeypatch.setattr(ai_paper, "_portfolio_plan", slow_plan)
    monkeypatch.setattr(ai_paper, "_PORTFOLIO_CACHE", {})
    client = TestClient(app)
    codes = []
    threads = [
        threading.Thread(target=lambda: codes.append(client.get("/api/fusion/ai/portfolio").status_code))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200] * 5
    assert len(calls) == 1


def test_shipped_portfolio_block_is_verified_and_unleveraged():
    from loki_v8.train import load_report

    block = load_report()["universal"]["portfolio"]
    assert block["verified"] and block["target_vol_pct"] in (10.0, 15.0, 20.0, 25.0)
    assert block["holdout"]["sharpe"] > 0.5
    assert 0 < block["average_gross_exposure"] <= 1.0


def test_bootstrap_interval_brackets_the_point_estimate():
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0005, 0.006, 900), index=pd.bdate_range("2023-01-02", periods=900))
    ci = portfolio_ai.bootstrap_ci(r, n=400)
    point = portfolio_ai.window_stats(r, "2000-01-01", "2100-01-01")
    assert ci["cagr_pct"][0] < point["cagr_pct"] < ci["cagr_pct"][1]
    assert 0 <= ci["probability_profitable"] <= 1


def test_health_flags_a_bad_year_against_its_own_history():
    rng = np.random.default_rng(2)
    good = pd.Series(rng.normal(0.0008, 0.006, 2600), index=pd.bdate_range("2010-01-01", periods=2600))
    ref = portfolio_ai.expected_range(good, "2010-01-01", "2030-01-01")
    assert ref["samples"] > 100 and ref["sharpe_p10"] < ref["sharpe_p90"]
    bad = pd.concat([good, pd.Series(rng.normal(-0.002, 0.008, 260), index=pd.bdate_range(good.index[-1] + pd.Timedelta(days=1), periods=260))])
    assert portfolio_ai.health(bad, ref)["status"] == "below_range"
    assert portfolio_ai.health(good, ref)["status"] in ("normal", "above_range")


def test_recent_changes_lists_entries_and_exits(sleeves):
    for s in sleeves.values():
        s["exposure"].iloc[-3:] = [0.0, 0.5, 0.0]
    changes = portfolio_ai.recent_changes(sleeves, bars=3)
    actions = {(c["action"]) for c in changes}
    assert actions == {"entered", "exited"}


def test_hindsight_check_reruns_the_method_on_unpicked_markets(sleeves):
    frames = {f"S{i}": _tape(900, seed=i + 3) for i in range(5)}
    idx = sleeves["S0"]["returns"].index
    windows = {"development": (str(idx[100].date()), str(idx[350].date())),
               "validation": (str(idx[351].date()), str(idx[480].date())),
               "holdout": (str(idx[481].date()), "2100-01-01")}
    out = portfolio_ai.hindsight_check(sleeves, frames, windows, symbols=("S0", "S1", "S2", "S3"))
    assert out["symbols"] == ["S0", "S1", "S2", "S3"] and out["target_vol_pct"] in (10.0, 15.0, 20.0, 25.0)
    assert {"holdout", "holdout_buy_hold", "verdict"} <= set(out)
    assert any(word in out["verdict"] for word in ("beats", "matches", "trails"))
    assert portfolio_ai.hindsight_check(sleeves, frames, windows, symbols=("S0", "X")) == {}  # too few markets


def test_shipped_record_carries_an_honest_hindsight_check():
    from loki_v8.train import load_report
    block = load_report()["universal"]["portfolio"]
    hc = block["hindsight_check"]
    assert not {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "JPM", "XOM"} & set(hc["symbols"])
    assert hc["holdout"]["max_dd_pct"] > hc["holdout_buy_hold"]["max_dd_pct"]  # smaller worst drop
    assert "buying and holding" in hc["verdict"]
    assert any("inverse volatility" in r for r in block["rejected_experiments"])


def test_idle_cash_earns_treasury_bill_interest(sleeves):
    plain = portfolio_ai.combine(sleeves, 10.0, warmup=260)
    idx = plain["returns"].index
    yields = pd.Series(5.0, index=idx)  # 5% a year
    parked = portfolio_ai.combine(sleeves, 10.0, warmup=260, cash_yield=yields)
    extra = (parked["returns"] - plain["returns"]).iloc[5:]
    cash = (1 - plain["gross"]).shift(1).iloc[5:]
    assert (extra[cash > 0.5] > 0).mean() > 0.9  # idle days earn interest
    daily = portfolio_ai.tbill_returns(idx, yields)
    assert daily.iloc[10] == pytest.approx((5.0 - portfolio_ai.CASH_FEE_PCT) / 100 / 365 * (idx[10] - idx[9]).days)
    alloc = portfolio_ai.allocation(sleeves, parked)
    park = alloc["cash_parking"]
    assert park["symbol"] == portfolio_ai.CASH_ETF and park["weight"] == pytest.approx(alloc["cash"], abs=1e-4)
    assert park["yield_pct"] == pytest.approx(5.0 - portfolio_ai.CASH_FEE_PCT)
    assert "cash_parking" not in portfolio_ai.allocation(sleeves, plain)  # no yields, no parking


def test_calendar_years_include_losing_years():
    idx = pd.bdate_range("2019-01-01", "2021-06-30")
    r = pd.Series(np.where(idx.year == 2020, -0.0005, 0.0004), index=idx)
    years = portfolio_ai.calendar_years(r, start="2019-01-01")
    assert [y["year"] for y in years] == [2019, 2020, 2021]
    assert years[1]["return_pct"] < 0 and years[2]["partial"] and not years[0]["partial"]


def test_shipped_record_parks_cash_and_reports_every_year():
    from loki_v8.train import load_report
    block = load_report()["universal"]["portfolio"]
    assert block["cash_parking"]["symbol"] == "SGOV" and 0 < block["cash_parking"]["average_cash"] < 1
    years = block["calendar_years"]
    assert years[0]["year"] == 2005 and any(y["return_pct"] < 0 for y in years)  # honest: not every year is up

"""The Day Trader's weekly self-review: walk-forward gate, shadow twin, and promotion only through a person."""

import numpy as np
import pandas as pd

import evolve_daytrade as ev
from loki_v8 import daytrade

CHAMP = "lookback 14, band 1"


def _book(days, means, seed=1):
    """Daily books for each setting, with the given mean daily return per setting and period."""
    idx = pd.bdate_range("2016-01-04", periods=days)
    rng = np.random.default_rng(seed)
    return {k: pd.Series(rng.normal(0, 0.004, days) + f(idx), index=idx) for k, f in means.items()}


def test_settings_stay_inside_the_tested_grid():
    assert daytrade.settings(None) == (14, 1.0)
    assert daytrade.settings({"lookback": 20, "band": 1.2}) == (20, 1.2)
    assert daytrade.settings({"lookback": 7, "band": 1.0}) == (14, 1.0)  # never an untested setting
    assert daytrade.settings({"lookback": 20, "band": 3.0}) == (14, 1.0)
    assert len(ev.CONTENDERS) == 9 and {"lookback": 14, "band": 1.0} in ev.CONTENDERS


def test_walk_forward_only_uses_the_past_and_passes_a_clearly_better_process():
    # One setting is better every month: the process picks it and beats the fixed rules on unseen months.
    good = _book(2700, {CHAMP: lambda i: 0.0, "lookback 20, band 1": lambda i: 0.0012 + 0 * i.year})
    wf = ev.walk_forward(good, CHAMP)
    assert wf["passed"] and wf["current_pick"] == "lookback 20, band 1" and wf["months_tested"] > 100
    # An edge that has gone away: the process notices within six months and goes back to the fixed rules.
    fading = _book(2700, {CHAMP: lambda i: 0.0002 + 0 * i.year,
                          "lookback 20, band 1": lambda i: np.where(i.year < 2024, 0.0015, -0.0015)})
    assert ev.walk_forward(fading, CHAMP)["current_pick"] == CHAMP
    # The gate itself: a better process overall that is worse lately does not pass.
    gate = {"adaptive_sharpe": 1.03, "champion_sharpe": 0.90, "adaptive_recent_sharpe": -0.63, "champion_recent_sharpe": -0.59}
    assert ev._why_not(gate) == "worse over the last 24 months: -0.63 vs -0.59"
    # No real difference: noise alone must not pass.
    same = _book(2700, {CHAMP: lambda i: 0.0003 + 0 * i.year, "lookback 10, band 1": lambda i: 0.0003 + 0 * i.year})
    assert not ev.walk_forward(same, CHAMP)["passed"]


def _bars(days=40, seed=4):
    rng = np.random.default_rng(seed)
    frames, price = [], 100.0
    for d in pd.bdate_range("2026-09-01", periods=days):
        idx = pd.date_range(f"{d.date()} 09:30", periods=78, freq="5min", tz="America/New_York")
        path = price * np.cumprod(1 + rng.normal(0, 0.0012, 78))
        opens = np.concatenate([[price], path[:-1]])
        frames.append(pd.DataFrame({"open": opens, "high": np.maximum(opens, path) * 1.0005, "low": np.minimum(opens, path) * 0.9995,
                                    "close": path, "volume": 1000.0}, index=idx))
        price = float(path[-1])
    return pd.concat(frames)


def test_shadow_needs_seven_sessions_and_a_positive_average_no_worse_than_the_rules():
    bars = _bars()
    days = [str(d.date()) for d, _ in daytrade.sessions(bars)]
    early = ev.shadow(["QQQ"], {"lookback": 10, "band": 0.8}, {"lookback": 14, "band": 1.0}, days[-4], lambda s: bars)
    assert not early["ready"] and not early["passed"] and early["sessions"] == 3
    full = ev.shadow(["QQQ"], {"lookback": 10, "band": 0.8}, {"lookback": 14, "band": 1.0}, days[-12], lambda s: bars)
    assert full["ready"] and full["sessions"] == 11
    cand, champ = full["candidate"]["mean_day_pct"], full["champion"]["mean_day_pct"]
    assert full["passed"] == (cand > 0 and cand >= champ)


def test_a_generation_moves_through_the_gates_and_never_promotes_by_itself(monkeypatch):
    record = {"symbols": ["QQQ"], "params": {"lookback": 14, "band": 1.0}, "data_through": "2026-11-06"}
    state = {"generations": []}
    gen = ev.generation(state, record, None, 1)
    assert "nothing tested" in gen["outcome"] and state.get("shadow") is None
    monkeypatch.setattr(ev, "books", lambda symbols, cache, workers: _book(2700, {CHAMP: lambda i: 0.0 * i.year,
                                                                                    "lookback 20, band 1": lambda i: 0.0012 + 0 * i.year}))
    gen = ev.generation(state, record, "cache", 1)
    assert "enters the shadow test" in gen["outcome"] and state["shadow"]["params"] == {"lookback": 20, "band": 1.0}
    monkeypatch.setattr(ev, "shadow", lambda *a, **k: {"ready": True, "passed": True, "sessions": 8,
                                                       "candidate": {"mean_day_pct": 0.1, "total_pct": 0.8},
                                                       "champion": {"mean_day_pct": 0.0, "total_pct": 0.0}})
    gen = ev.generation(state, record, "cache", 1)
    assert "proposed for promotion" in gen["outcome"] and state["promote"]["params"] == {"lookback": 20, "band": 1.0}
    assert record["params"] == {"lookback": 14, "band": 1.0}  # the live rules only change when the record is rebuilt and merged
    assert "Shadow test" in ev.summary(gen) and len(state["generations"]) == 3


def test_a_failed_shadow_test_is_logged_and_dropped(monkeypatch):
    record = {"symbols": ["QQQ"], "params": None, "data_through": "2026-11-06"}
    state = {"generations": [], "shadow": {"params": {"lookback": 10, "band": 1.2}, "since": "2026-11-06"}}
    monkeypatch.setattr(ev, "shadow", lambda *a, **k: {"ready": True, "passed": False, "sessions": 9,
                                                       "candidate": {"mean_day_pct": -0.05, "total_pct": -0.4},
                                                       "champion": {"mean_day_pct": 0.02, "total_pct": 0.2}})
    gen = ev.generation(state, record, None, 1)
    assert "shadow test failed" in gen["outcome"] and state["shadow"] is None and "promote" not in state


def test_shipped_self_review_log_is_honest():
    import json
    from pathlib import Path
    state = json.loads((Path(__file__).resolve().parents[1] / "loki_v8" / "daytrade_evolution.json").read_text())
    first = state["generations"][0]
    assert first["walk_forward"]["months_tested"] > 100 and len(first["walk_forward"]["fixed_settings"]) == 9
    assert state["champion"] == {"lookback": 14, "band": 1.0} and not state.get("promote")

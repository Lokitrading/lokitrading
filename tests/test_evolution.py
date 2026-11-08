"""Self-evolution: champion vs challengers, with promotion rules that resist overfitting."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from loki.api.server import app
from loki_v8 import evolution

GRID = {
    "preset": ["balanced", "trend_leader", "trend_only"],
    "signal_threshold": [3.0, 8.0, 15.0, 30.0],
    "stop_loss_atr": [3.0, 6.0, 10.0],
    "trailing_stop_atr": [0.0, 8.0],
    "target_vol_pct": [12.0, 20.0, 30.0],
    "exit_mode": ["signal", "trend"],
}
CHAMP = {"preset": "trend_only", "signal_threshold": 15.0, "stop_loss_atr": 6.0, "trailing_stop_atr": 0.0,
         "target_vol_pct": 20.0, "exit_mode": "trend"}


def entry(rank, holdout=0.5, recent=0.3, params=None):
    return {"params": params or CHAMP, "rank_score": rank, "holdout_score": holdout, "recent_score": recent}


def test_grid_params_reads_a_stored_profile():
    stored = {"factor_preset": "trend_only", "signal_threshold": 30.0, "stop_loss_atr": 10.0, "trailing_stop_atr": 0.0,
              "target_vol_pct": 20.0, "exit_mode": "trend", "sizing_mode": "vol_target", "meta_filter": {"threshold": 0.3}}
    assert evolution.grid_params(stored) == {"preset": "trend_only", "signal_threshold": 30.0, "stop_loss_atr": 10.0,
                                             "trailing_stop_atr": 0.0, "target_vol_pct": 20.0, "exit_mode": "trend"}


def test_neighbours_change_exactly_one_setting_by_one_step():
    found = evolution.neighbours(CHAMP, GRID)
    for n in found:
        assert sum(n[k] != CHAMP[k] for k in CHAMP) == 1
    thresholds = {n["signal_threshold"] for n in found if n["signal_threshold"] != CHAMP["signal_threshold"]}
    assert thresholds == {8.0, 30.0}  # one step either side, never a jump to 3.0
    assert {n["preset"] for n in found} >= {"balanced", "trend_leader"}  # categorical: every alternative


def test_challengers_are_unique_never_the_champion_and_reproducible():
    a = evolution.challengers(CHAMP, GRID, n_random=6, seed=202645)
    b = evolution.challengers(CHAMP, GRID, n_random=6, seed=202645)
    assert a == b
    keys = [json.dumps(c, sort_keys=True) for c in a]
    assert len(keys) == len(set(keys))
    assert json.dumps(CHAMP, sort_keys=True) not in keys
    assert len(a) == len(evolution.neighbours(CHAMP, GRID)) + 6


def test_champion_stays_unless_a_challenger_is_clearly_better():
    d = evolution.decide(entry(0.50), [entry(0.53), entry(0.40)])
    assert not d["promoted"] and d["winner"]["rank_score"] == 0.50
    assert "not the required" in d["reason"]


def test_clear_winner_that_holds_up_on_recent_data_is_promoted():
    d = evolution.decide(entry(0.50), [entry(0.60, holdout=0.52, recent=0.31), entry(0.55)])
    assert d["promoted"] and d["winner"]["rank_score"] == 0.60


def test_recent_data_can_veto_an_overfit_challenger():
    d = evolution.decide(entry(0.50, holdout=0.5), [entry(0.70, holdout=0.30)])
    assert not d["promoted"] and "overfit" in d["reason"]
    d = evolution.decide(entry(0.50, recent=0.3), [entry(0.70, recent=0.1)])
    assert not d["promoted"] and "last-12-months" in d["reason"]


def test_no_challengers_keeps_the_champion():
    d = evolution.decide(entry(0.5), [])
    assert not d["promoted"] and d["winner"]["rank_score"] == 0.5


def test_log_numbers_generations_counts_promotions_and_stays_bounded():
    log = {}
    for i in range(evolution.MAX_LOG_ENTRIES + 5):
        log = evolution.append_log(log, {"date": f"d{i}", "equity": {"promoted": i == 3}, "crypto": {"promoted": False}})
    gens = log["generations"]
    assert len(gens) == evolution.MAX_LOG_ENTRIES
    assert gens[-1]["generation"] == evolution.MAX_LOG_ENTRIES + 5
    assert log["promotions"] == 0  # generation 4 has scrolled out of the bounded window
    s = evolution.summary(log)
    assert s["generations"] == evolution.MAX_LOG_ENTRIES and len(s["history"]) == 12


def test_shipped_log_matches_the_shipped_snapshot():
    root = Path(__file__).resolve().parents[1] / "loki_v8"
    log = json.loads((root / "evolution_log.json").read_text(encoding="utf-8"))
    snap = json.loads((root / "training_report_snapshot.json").read_text(encoding="utf-8"))["universal"]
    latest = log["generations"][-1]
    for kind in ("equity", "crypto"):
        winner = latest[kind]["winner"]["params"]
        assert evolution.grid_params(snap[kind]["params"]) == winner, kind
        assert latest[kind]["data_through"] == snap[kind]["data_through"]
        assert snap[kind]["verified"]
    meta = snap["equity"]["params"].get("meta_filter")
    if latest["equity"]["mistakes"]["kept"]:
        assert meta and meta["trained_on_trades_closed_before"] == latest["equity"]["data_through"]


def test_evolution_endpoint_serves_the_log():
    data = TestClient(app).get("/api/fusion/ai/evolution").json()
    assert data["generations"] >= 1 and data["latest"]["equity"]["reason"]
    assert "hypothetical" in data["how_it_works"]

"""Loki's memory: remember, grade, learn lessons, and act only on lessons proven on unseen decisions."""
from __future__ import annotations

import json
import random

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from loki_v8 import mind


def dec(d, s, held, fwd, regime="BULL", score=40.0, w=1.0):
    return {"d": d, "s": s, "held": held, "w": w, "score": score, "regime": regime, "px": 100.0,
            "fwd": fwd, "verdict": ("right" if fwd > 0 else "mistake") if held else "right"}


def test_grading_marks_right_mistakes_and_missed_moves():
    idx = pd.bdate_range("2025-01-01", periods=12)
    closes = {"SPY": pd.Series(np.linspace(100, 111, 12), index=idx),
              "BTC-USD": pd.Series(np.linspace(100, 89, 12), index=idx),
              "ETH-USD": pd.Series(np.linspace(100, 120, 12), index=idx)}
    day = str(idx[0].date())
    decisions = [{"d": day, "s": "SPY", "held": True, "w": 1, "score": 50, "regime": "BULL", "px": 100.0},
                 {"d": day, "s": "BTC-USD", "held": True, "w": 1, "score": 50, "regime": "BULL", "px": 100.0},
                 {"d": day, "s": "ETH-USD", "held": False, "w": 0, "score": -5, "regime": "BEAR", "px": 100.0},
                 {"d": str(idx[-1].date()), "s": "SPY", "held": True, "w": 1, "score": 50, "regime": "BULL", "px": 111.0}]
    g = mind.grade(decisions, closes, horizon=5)
    assert [x["verdict"] for x in g] == ["right", "mistake", "missed", "pending"]
    assert g[0]["fwd"] == pytest.approx(0.05)


def test_decisions_use_only_what_was_known_that_day():
    idx = pd.bdate_range("2025-01-01", periods=30)
    sleeve = {"close": pd.Series(np.arange(100, 130, dtype=float), index=idx),
              "exposure": pd.Series([0.0] * 15 + [0.8] * 15, index=idx),
              "score": pd.Series(np.arange(30, dtype=float), index=idx),
              "regime": pd.Series(["RANGING"] * 30, index=idx)}
    out = mind.decisions_from_sleeves({"SPY": sleeve}, lookback=30, stride=5)
    assert [o["d"] for o in out] == [str(idx[i].date()) for i in range(0, 30, 5)]
    assert [o["held"] for o in out] == [False, False, False, True, True, True]
    assert out[3]["score"] == 15.0 and out[3]["px"] == 115.0 and out[3]["regime"] == "RANGING"


def planted(seed=1, weak_loses=True, n_dates=80):
    """Stocks win a little; crypto in a bull regime keeps losing (if weak_loses), in both halves."""
    rng = random.Random(seed)
    rows = []
    for i in range(n_dates):
        d = str((pd.Timestamp("2024-01-01") + pd.Timedelta(days=7 * i)).date())
        for s in ("SPY", "QQQ", "AAPL"):
            rows.append(dec(d, s, True, rng.gauss(0.004, 0.02), regime="RANGING"))
        for s in ("BTC-USD", "ETH-USD"):
            drift = -0.02 if weak_loses else 0.004
            rows.append(dec(d, s, True, rng.gauss(drift, 0.02), regime="BULL"))
    return rows


def test_a_weak_spot_is_found_and_described():
    les = mind.lessons(planted())
    weak = {w["group"] for w in les["weak_spots"]}
    assert "crypto in bull regime" in weak
    text = next(w["lesson"] for w in les["weak_spots"] if w["group"] == "crypto in bull regime")
    assert text.startswith("Holding when crypto in bull regime: right") and "average week -" in text


def test_a_lesson_that_keeps_holding_is_proven_and_adopted():
    proof = mind.prove(planted())
    assert proof["status"] == "proven" and "crypto in bull regime" in proof["adopted"]
    assert proof["evidence"]["gain_pct_points"] > 0 and proof["evidence"]["beats_shuffles_pct"] >= 90


def test_noise_is_never_adopted():
    for seed in range(5):
        proof = mind.prove(planted(seed=seed, weak_loses=False))
        assert proof["status"] != "proven" and proof["adopted"] == []


def test_a_lesson_that_stops_working_later_is_not_adopted():
    rows = planted(n_dates=80)
    cut = sorted({r["d"] for r in rows})[48]
    for r in rows:  # crypto-in-bull stopped losing after the learning period
        if r["d"] >= cut and r["s"].endswith("-USD"):
            r["fwd"] = abs(r["fwd"]) + 0.01
    proof = mind.prove(rows)
    assert proof["status"] == "not proven" and proof["adopted"] == []


def test_not_enough_decisions_means_still_learning():
    assert mind.prove(planted(n_dates=6))["status"] == "learning"


def test_recent_form_reports_slipping():
    rows = [dec(f"2025-01-{1 + i % 28:02d}", "SPY", True, 0.01 if i < 200 else -0.01) for i in range(260)]
    form = mind.recent_form(rows, last=60)
    assert form["status"] == "slipping" and form["recent"]["right_pct"] == 0.0


class TestThePlan:
    def alloc(self):
        return {"gross": 0.6, "cash": 0.4, "positions": [
            {"symbol": "BTC-USD", "weight": 0.2, "regime": "BULL", "score": 40},
            {"symbol": "SPY", "weight": 0.4, "regime": "RANGING", "score": 40}]}

    def test_only_proven_lessons_change_the_plan(self):
        from loki.api import ai_paper

        unproven = ai_paper._apply_memory(self.alloc(), {"proof": {"status": "not proven", "adopted": ["crypto in bull regime"]}})
        assert [p["weight"] for p in unproven["positions"]] == [0.2, 0.4] and unproven["memory"]["cautioned"] == []
        proven = ai_paper._apply_memory(self.alloc(), {"proof": {"status": "proven", "adopted": ["crypto in bull regime"]}})
        assert proven["positions"][0]["weight"] == 0.1 and proven["positions"][0]["memory_caution"] == "crypto in bull regime"
        assert proven["positions"][1]["weight"] == 0.4 and proven["gross"] == 0.5 and proven["cash"] == 0.5


def synthetic_sleeves(n=600):
    idx = pd.bdate_range("2023-01-02", periods=n)
    rng = np.random.default_rng(3)
    out = {}
    for s in ("SPY", "QQQ", "BTC-USD", "GLD"):
        close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n))), index=idx)
        score = pd.Series(rng.normal(20, 40, n), index=idx)
        out[s] = {"close": close, "score": score, "exposure": (score > 0).astype(float),
                  "regime": pd.Series(rng.choice(["BULL", "RANGING", "BEAR"], n), index=idx),
                  "returns": close.pct_change().fillna(0),
                  "state": {"as_of": str(idx[-1].date()), "last_close": float(close.iloc[-1]), "score": 10.0, "regime": "BULL"}}
    return out


def test_memory_endpoint_and_desktop_journal(monkeypatch, tmp_path):
    from loki.api import ai_paper
    from loki.api.server import app

    inputs = {"sleeves": synthetic_sleeves()}
    monkeypatch.setattr(ai_paper, "_portfolio_inputs", lambda: inputs)
    ai_paper._MEMORY_CACHE.clear()
    monkeypatch.setenv("LOKI_DESKTOP", "1")
    monkeypatch.setenv("LOKI_DATA_DIR", str(tmp_path))
    c = TestClient(app)
    body = c.get("/api/fusion/ai/memory").json()
    assert body["remembered"] > 300 and body["graded"] > 300
    assert set(body["verdicts"]) <= {"right", "mistake", "missed"}
    assert body["proof"]["status"] in ("proven", "not proven", "no weak spots", "learning")
    assert body["journal"]["decisions"] == 4  # today's decision for each market, kept on this computer
    again = c.get("/api/fusion/ai/memory").json()
    assert again["journal"]["decisions"] == 4  # the same day is never written twice
    assert len(json.loads((tmp_path / "mind_journal.json").read_text())) == 4
    monkeypatch.delenv("LOKI_DESKTOP")
    assert c.get("/api/fusion/ai/memory").json()["journal"] is None  # the website keeps no journal


def test_talk_to_loki_answers_from_its_memory(monkeypatch):
    from loki.api import ai_paper, loki_brain

    monkeypatch.setattr(ai_paper, "_portfolio_inputs", lambda: {"sleeves": synthetic_sleeves()})
    ai_paper._MEMORY_CACHE.clear()
    reply = loki_brain.answer("what have you learned from your mistakes?")
    assert reply["intent"] == "memory"
    assert "I remember" in reply["text"] and "graded" in reply["text"] and "proven" in reply["text"]


def test_the_workspace_shows_the_memory_panel():
    from loki.api.server import app

    html = TestClient(app).get("/").text
    assert 'id="mind"' in html and "/static/js/mind_view.js" in html

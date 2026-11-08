"""Prediction markets: read-only Polymarket relay, Crowd vs Machine maths, odds in chat."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from loki.api import loki_brain, polymarket as pm
from loki.api.server import app

client = TestClient(app)


def market(label, yes, mid="1", closed=False):
    return {"id": mid, "groupItemTitle": label, "question": f"Q {label}", "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 4))]), "active": True, "closed": closed,
            "oneDayPriceChange": 0.01, "volume24hr": 100}


def event(title, markets, days=5, slug="some-event"):
    end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {"id": "9", "title": title, "slug": slug, "endDate": end, "volume24hr": 5000, "volume": 10000,
            "liquidity": 2000, "markets": markets}


@pytest.mark.parametrize("title,symbol", [
    ("Bitcoin above ___ on November 11?", "BTC-USD"),
    ("NVIDIA (NVDA) closes above ___ on November 9?", "NVDA"),
    ("Will Apple (AAPL) finish week of November 9 above___?", "AAPL"),
    ("What price will Bitcoin hit in November?", None),
    ("Fed Decision in December?", None),
])
def test_ladder_titles(title, symbol):
    assert pm.ladder_symbol(title) == symbol


def test_crowd_curve_is_monotone_and_reads_median_and_odds():
    ms = [pm._clean_market(m) for m in (market("$740", 0.3), market("$730", 0.75), market("$735", 0.5),
                                         market("$745", 0.32), market("$725", 0.9))]
    curve = pm.crowd_curve(ms)
    assert [lvl for lvl, _ in curve] == [725, 730, 735, 740, 745]
    assert all(a[1] >= b[1] for a, b in zip(curve, curve[1:]))  # the noisy 0.32 above 0.30 is smoothed
    assert pm.crowd_median(curve) == pytest.approx(735.0)
    assert pm.prob_above(curve, 732.5) == pytest.approx(0.625)
    assert pm.lean(0.62) == "bullish" and pm.lean(0.40) == "bearish" and pm.lean(0.5) == "neutral"


def test_thresholds_parse_dollars_commas_and_k():
    assert pm._threshold("76,000") == 76000 and pm._threshold("$1.5k") == 1500 and pm._threshold("$185") == 185


def test_machine_lean_reads_the_ai_plan():
    assert pm.machine_lean({"plan": {"allowed": True}})[0] == "bullish"
    assert pm.machine_lean({"plan": {"allowed": False, "action": "SELL", "score": -40}})[0] == "bearish"
    assert pm.machine_lean({"plan": {"allowed": False, "score": 12}})[0] == "neutral"
    assert pm.machine_lean(None)[0] == "unknown"


def test_clean_event_drops_closed_and_whitelists_links():
    e = pm._clean_event(event("Fed Decision in December?", [market("No change", 0.2), market("25 bps hike", 0.77, "2"),
                                                             market("Gone", 0.5, "3", closed=True)],
                              slug="fed-decision-in-december<script>"))
    assert [m["label"] for m in e["markets"]] == ["25 bps hike", "No change"]  # most likely first, closed dropped
    assert e["url"] == "https://polymarket.com/event/fed-decision-in-decemberscript"


def test_events_endpoint_and_errors(monkeypatch):
    monkeypatch.setattr(pm, "_get", lambda path, params: [event("Fed Decision in December?", [market("No change", 0.2)])])
    data = client.get("/api/polymarket/events?category=economy").json()
    assert data["events"][0]["title"] == "Fed Decision in December?" and "never places" in data["note"]
    assert client.get("/api/polymarket/events?category=casino").status_code == 422

    def down(path, params):
        raise pm.PolymarketError("Polymarket is unreachable right now")
    monkeypatch.setattr(pm, "_get", down)
    response = client.get("/api/polymarket/events")
    assert response.status_code == 503 and "nothing is guessed" in response.json()["detail"]


def test_markets_endpoint_only_takes_numeric_ids(monkeypatch):
    monkeypatch.setattr(pm, "_get", lambda path, params: [market("x", 0.4, params["id"])])
    assert client.get("/api/polymarket/markets?ids=12,34").json()["markets"][0]["id"] == "12"
    assert client.get("/api/polymarket/markets?ids=../etc").status_code == 422


def test_crowd_vs_machine_compares_both_sides(monkeypatch):
    ladder = event("NVIDIA (NVDA) closes above ___ on November 20?",
                   [market("$180", 0.9, "1"), market("$185", 0.7, "2"), market("$190", 0.4, "3"), market("$195", 0.1, "4")])
    monkeypatch.setattr(pm, "_get", lambda path, params: [ladder] if params.get("tag_slug") == "stocks" else [])
    monkeypatch.setattr(pm, "_price_and_read", lambda s: (186.0, {"plan": {"allowed": False, "action": "SELL", "score": -50}}))
    items = client.get("/api/polymarket/crowd-vs-machine").json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["symbol"] == "NVDA" and item["crowd_lean"] == "bullish" and item["machine_lean"] == "bearish"
    assert item["verdict"] == "disagree" and item["crowd_prob_up"] == pytest.approx(0.64, abs=0.01)
    assert 186 < item["crowd_median"] < 190


def test_talk_to_loki_answers_odds_from_polymarket(monkeypatch):
    found = [{"title": "Fed Decision in December?", "end": "2026-12-10T00:00:00Z",
              "markets": [{"label": "25 bps increase", "yes": 0.765}, {"label": "No change", "yes": 0.215}]},
             {"title": "How many dissent at the December Fed meeting?", "end": None, "markets": [{"label": "0", "yes": 0.7}]}]
    seen = []
    monkeypatch.setattr(loki_brain, "_poly_search", lambda q: seen.append(q) or list(reversed(found)))
    reply = loki_brain.answer("will the fed cut rates in december?")
    assert reply["intent"] == "odds" and seen[-1] == "fed decision december"
    assert reply["text"].startswith("Polymarket traders currently price “Fed Decision in December?”")
    assert "25 bps increase 76.5%" in reply["text"] and "not Loki's prediction" in reply["text"]


def test_no_odds_found_falls_through(monkeypatch):
    monkeypatch.setattr(loki_brain, "_poly_search", lambda q: [])
    assert (loki_brain.answer("what are the odds of a recession") or {}).get("intent") != "odds"

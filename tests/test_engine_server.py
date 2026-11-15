"""Server pieces of the on-device engine: replay history relay, stateless Tradovate relay,
CSP for device-direct feeds, and the workspace wiring for the new views."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loki.api import broker_tradovate, replay_data
from loki.api.server import CONTENT_SECURITY_POLICY, app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def chart(stamps, closes):
    return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{
        "open": closes, "high": [c * 1.01 if c else None for c in closes], "low": [c * 0.99 if c else None for c in closes],
        "close": closes, "volume": [100] * len(closes)}]}}]}}


def test_parse_chart_drops_junk_rows():
    rows = replay_data.parse_chart(chart([1, 2, 3, 4], [10.0, None, -5.0, 11.0]))
    assert [r["t"] for r in rows] == [1000, 4000] and rows[1]["c"] == 11.0


def test_replay_bars_validates_windows_and_caches_finished_history(monkeypatch):
    calls = []
    monkeypatch.setattr(replay_data, "_http_json", lambda url, timeout=8.0: calls.append(url) or chart(list(range(1, 11)), [100.0 + i for i in range(10)]))
    ok = client.get("/api/replay/bars?symbol=SPY&start=2020-02-01&end=2020-05-01&interval=1d")
    assert ok.status_code == 200 and len(ok.json()["bars"]) == 10
    assert "immutable" in ok.headers["cache-control"] and "period1=" in calls[0]
    assert client.get("/api/replay/bars?symbol=SPY&start=2020-05-01&end=2020-02-01").status_code == 422
    assert client.get("/api/replay/bars?symbol=SPY&start=2015-01-01&end=2020-01-01").status_code == 422  # > 3 years daily
    assert client.get("/api/replay/bars?symbol=SPY&start=2019-01-01&end=2019-01-20&interval=1h").status_code == 422  # too old for hourly
    assert client.get("/api/replay/bars?symbol=../etc&start=2020-01-01&end=2020-02-01").status_code == 422


def test_tradovate_token_is_relayed_not_stored(monkeypatch):
    monkeypatch.setattr(broker_tradovate.tradovate, "request_token", lambda env, name, pw, cid, sec, app_id: ("tok-" + "x" * 30, 123))
    res = client.post("/api/broker/tradovate/token", json={"environment": "demo", "name": "u", "password": "p", "cid": 7, "sec": "secret-123"})
    body = res.json()
    assert res.status_code == 200 and body["access_token"].startswith("tok-") and "never stores" in body["note"]
    assert "set-cookie" not in res.headers and res.headers["cache-control"] == "no-store"
    assert client.post("/api/broker/tradovate/token", json={"environment": "paper", "name": "u", "password": "p", "cid": 7, "sec": "secret-123"}).status_code == 422


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


def test_tradovate_orders_need_a_token_and_live_needs_the_phrase(monkeypatch):
    sent = []
    monkeypatch.setattr(broker_tradovate.requests, "request", lambda method, url, **kw: sent.append((method, url, kw.get("json"))) or FakeResponse({"orderId": 99}))
    order = {"account_id": 1, "account_spec": "DEMO123", "symbol": "MESZ6", "action": "Buy", "quantity": 1, "order_type": "Market"}
    auth = {"Authorization": "Bearer " + "t" * 40}
    assert client.post("/api/broker/tradovate/demo/order", json=order).status_code == 401
    demo = client.post("/api/broker/tradovate/demo/order", json=order, headers=auth)
    assert demo.status_code == 200 and demo.json()["order_id"] == 99
    assert sent[-1][1].startswith("https://demo.tradovateapi.com/v1/order/placeorder") and sent[-1][2]["isAutomated"] is True
    assert client.post("/api/broker/tradovate/live/order", json=order, headers=auth).status_code == 428
    live = client.post("/api/broker/tradovate/live/order", json={**order, "confirm": "Trade Real Money"}, headers=auth)
    assert live.status_code == 200 and sent[-1][1].startswith("https://live.tradovateapi.com/")
    assert client.post("/api/broker/tradovate/demo/order", json={**order, "quantity": 11}, headers=auth).status_code == 422
    assert client.post("/api/broker/tradovate/demo/order", json={**order, "order_type": "Limit"}, headers=auth).status_code == 422
    monkeypatch.setattr(broker_tradovate.requests, "request", lambda method, url, **kw: FakeResponse({"failureReason": "RiskCheck", "failureText": "Insufficient margin"}))
    refused = client.post("/api/broker/tradovate/demo/order", json=order, headers=auth)
    assert refused.status_code == 422 and "Insufficient margin" in refused.json()["detail"]


def test_tradovate_flatten_reports_each_contract(monkeypatch):
    monkeypatch.setattr(broker_tradovate.requests, "request", lambda method, url, **kw: FakeResponse({"ok": True}) if kw["json"]["contractId"] == 1 else FakeResponse({}, 500))
    res = client.post("/api/broker/tradovate/demo/flatten", json={"account_id": 5, "contract_ids": [1, 2]}, headers={"Authorization": "Bearer " + "t" * 40})
    assert [r["ok"] for r in res.json()["results"]] == [True, False]


def test_csp_allows_only_the_device_direct_feeds():
    assert "connect-src 'self' https://gamma-api.polymarket.com" in CONTENT_SECURITY_POLICY
    for host in ("https://clob.polymarket.com", "https://data-api.binance.vision", "https://paper-api.alpaca.markets",
                 "https://api.alpaca.markets", "wss://data-stream.binance.vision"):
        assert host in CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY and "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "worker-src 'self'" in CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("view", ["trade", "council", "replay", "execution-map"])
def test_new_views_are_wired(view):
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert f'<section id="{view}" class="workspace-view"' in html and f'data-view="{view}"' in html


def test_engine_scripts_ship_and_three_is_vendored_with_its_licence():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    for f in ("engine/core.js", "engine/council.js", "engine/swarm.js", "engine/risk.js", "engine/mc_core.js", "engine/montecarlo.js",
              "engine/arb.js", "engine/audit.js", "engine/copilot.js", "engine/replay.js", "engine/execmap.js", "trade.js", "copilot_bar.js"):
        assert f'/static/js/{f}?v={{{{ asset }}}}' in html, f
        assert (ROOT / "static" / "js" / f).is_file()
    assert (ROOT / "static" / "js" / "engine" / "mc_worker.js").is_file()
    assert "MIT" in (ROOT / "static" / "vendor" / "three" / "LICENSE").read_text(encoding="utf-8")
    assert html.index("engine/core.js") < html.index("trade.js")  # the engine loads before its views

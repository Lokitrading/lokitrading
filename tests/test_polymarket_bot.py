"""Polymarket paper bot (fair value vs the live book, exits first) and the feed status lights."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from loki.api import feed_status, polymarket as pm, polymarket_bot as bot
from loki.api.server import app

client = TestClient(app)
YES, NO = "1" * 20, "2" * 20


@pytest.fixture(autouse=True)
def _clear_caches():
    bot._book_cache.clear()
    bot._vol_cache.clear()
    feed_status._cache.clear()
    yield


# --- pure maths ------------------------------------------------------------------
def test_fair_prob_is_a_sane_lognormal():
    assert bot.fair_prob_above(100, 100, 0.5, 1 / 365) == pytest.approx(0.5, abs=0.01)
    deep_in, deep_out = bot.fair_prob_above(120, 100, 0.3, 7 / 365), bot.fair_prob_above(80, 100, 0.3, 7 / 365)
    assert deep_in > 0.99 and deep_out < 0.01
    # More time or more volatility pulls an in-the-money chance back towards a coin flip.
    assert bot.fair_prob_above(105, 100, 0.6, 30 / 365) < bot.fair_prob_above(105, 100, 0.3, 30 / 365)
    assert bot.fair_prob_above(105, 100, 0.3, 60 / 365) < bot.fair_prob_above(105, 100, 0.3, 5 / 365)
    assert bot.fair_prob_above(0, 100, 0.3, 1) is None and bot.fair_prob_above(100, 100, 0.3, 0) is None


def test_fee_walk_and_kelly():
    assert bot.taker_fee_per_share(0.2, None) == 0 and bot.taker_fee_per_share(0.8, 100) == pytest.approx(0.002)
    avg, worst, filled = bot.walk([(0.40, 10), (0.42, 10), (0.50, 100)], 15)
    assert filled == 15 and worst == 0.42 and avg == pytest.approx((10 * 0.40 + 5 * 0.42) / 15)
    assert bot.walk([(0.40, 3)], 10)[2] == 3  # partial: caller checks the size
    assert bot.walk([], 10) is None
    assert bot.kelly_stake(0.1, 0.5, 1000) == pytest.approx(1000 * 0.25 * 0.1 / 0.5)
    assert bot.kelly_stake(0.5, 0.4, 1000) == 1000 * bot.MAX_POSITION_FRACTION  # capped
    assert bot.kelly_stake(-0.1, 0.5, 1000) == 0 and bot.kelly_stake(0.1, 1.0, 1000) == 0


@pytest.mark.parametrize("bid,fair,seconds,expected", [
    (0.60, 0.70, 10 * 60, "lock"),        # lock beats everything, even a winning position
    (0.25, 0.90, 86400, "stop-loss"),     # 0.25 <= 0.50 * 0.6
    (0.85, 0.95, 86400, "take-profit"),   # 0.85 >= 0.5 + 0.6 * 0.5
    (0.55, 0.50, 86400, "edge gone"),     # model now below the bid
    (0.55, 0.70, 86400, "edge intact"),
    (None, 0.70, 86400, "no bids"),
])
def test_exit_rules_in_priority_order(bid, fair, seconds, expected):
    action, reason = bot.exit_decision({"entry": 0.5}, bid, fair, seconds)
    assert expected in reason
    assert action == ("hold" if expected in ("edge intact", "no bids") else "sell")


def test_choose_limits_concentration_and_cash():
    def c(symbol, resolves, edge, cost=10.0):
        return {"symbol": symbol, "resolves": resolves, "edge": edge, "cost": cost}
    picked = bot.choose([c("BTC-USD", "a", 0.2), c("BTC-USD", "a", 0.19), c("BTC-USD", "b", 0.1),
                         c("BTC-USD", "c", 0.09), c("NVDA", "a", 0.05, cost=500)], room=100)
    assert [(p["symbol"], p["resolves"]) for p in picked] == [("BTC-USD", "a"), ("BTC-USD", "b")]


# --- live data, mocked -------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


def test_book_sorts_best_first_and_validates_token(monkeypatch):
    raw = {"bids": [{"price": "0.40", "size": "5"}, {"price": "0.45", "size": "7"}, {"price": "bad", "size": "1"}],
           "asks": [{"price": "0.60", "size": "9"}, {"price": "0.52", "size": "4"}], "tick_size": "0.01", "min_order_size": "5"}
    calls = []
    monkeypatch.setattr(bot.requests, "get", lambda url, **kw: calls.append(kw["params"]) or FakeResponse(raw))
    b = bot.book(YES)
    assert b["bids"][0] == (0.45, 7) and b["asks"][0] == (0.52, 4) and b["tick"] == 0.01 and b["min_size"] == 5
    bot.book(YES)
    assert len(calls) == 1  # cached
    with pytest.raises(pm.PolymarketError):
        bot.book("../../etc")


def ladder_event(days=3, bid=0.30, ask=0.33):
    end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    market = {"id": "77", "groupItemTitle": "$110", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.32", "0.68"]',
              "clobTokenIds": json.dumps([YES, NO]), "bestBid": bid, "bestAsk": ask, "acceptingOrders": True, "active": True}
    return {"id": "9", "title": "NVIDIA (NVDA) closes above ___ on November 20?", "slug": "nvda-above", "endDate": end,
            "markets": [market]}


def wire(monkeypatch, spot=112.0, sigma=0.4, asks=((0.33, 400.0),), bids=((0.30, 400.0),), events=None):
    monkeypatch.setattr(pm, "_get", lambda path, params: (events if events is not None else [ladder_event()])
                        if params.get("tag_slug") == "stocks" else [])
    monkeypatch.setattr(bot, "_spot", lambda s: spot)
    monkeypatch.setattr(bot, "realized_vol", lambda s: sigma)
    monkeypatch.setattr(bot, "book", lambda t: {"asks": list(asks), "bids": list(bids), "tick": 0.01, "min_size": 5.0})


def test_scan_buys_the_underpriced_yes_token_through_the_book(monkeypatch):
    wire(monkeypatch)
    data = bot.scan(1000)
    assert data["ladders_scanned"] == 1 and len(data["signals"]) == 1
    s = data["signals"][0]
    raw = bot.fair_prob_above(112, 110, 0.4, bot._tau_years(3 * 86400))
    assert s["token"] == "YES" and s["token_id"] == YES and s["symbol"] == "NVDA"
    assert s["model_prob"] == pytest.approx(0.5 * raw + 0.5 * 0.315, abs=1e-3)
    assert s["edge"] >= bot.MIN_EDGE and s["avg_fill"] == 0.33
    assert s["cost"] <= 1000 * bot.MAX_POSITION_FRACTION + 0.01
    assert s["order"] == {"token_id": YES, "side": "BUY", "price": 0.33, "size": s["shares"], "order_type": "FOK"}


def test_scan_buys_no_when_the_model_is_below_the_crowd(monkeypatch):
    wire(monkeypatch, spot=100.0, events=[ladder_event(bid=0.60, ask=0.62)], asks=((0.40, 400.0),))
    s = bot.scan(1000)["signals"][0]
    assert s["token"] == "NO" and s["token_id"] == NO and "below" in s["thesis"]


@pytest.mark.parametrize("change", [
    {"events": [ladder_event(bid=0.20, ask=0.40)]},   # spread too wide to bother
    {"events": [ladder_event(days=0.1)]},             # resolves too soon
    {"events": [ladder_event(days=60)]},              # too far out
    {"asks": ((0.33, 2.0),)},                         # book too thin for the minimum size
    {"asks": ((0.48, 400.0),)},                       # quoted price looked good, the real book does not
    {"spot": 109.0},                                  # half model, half crowd: too close to the ask
])
def test_scan_sits_out_when_there_is_no_real_edge(monkeypatch, change):
    wire(monkeypatch, **change)
    assert bot.scan(1000)["signals"] == []


def test_scan_respects_existing_exposure(monkeypatch):
    wire(monkeypatch)
    assert bot.scan(1000, exposure=300)["signals"] == [] and bot.scan(1000, exposure=300)["room"] == 0


def position(**kw):
    p = {"id": "p1", "token_id": YES, "token": "YES", "symbol": "NVDA", "strike": 110, "entry": 0.33, "shares": 20,
         "resolves": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")}
    p.update(kw)
    return p


def test_review_walks_bids_and_emits_sell_orders(monkeypatch):
    wire(monkeypatch, bids=((0.15, 50.0),), asks=((0.17, 50.0),))
    out = bot.review([position()])[0]
    assert out["action"] == "sell" and "stop-loss" in out["reason"]
    assert out["order"] == {"token_id": YES, "side": "SELL", "price": 0.15, "size": 20.0, "order_type": "FOK"}

    wire(monkeypatch, bids=((0.36, 50.0),), asks=((0.38, 50.0),))
    assert bot.review([position()])[0]["action"] == "hold"

    soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    assert "locks" in bot.review([position(resolves=soon)])[0]["reason"]


def test_routes_are_paper_and_validate_input(monkeypatch):
    wire(monkeypatch)
    data = client.get("/api/polymarket/bot/signals?bankroll=1000").json()
    assert data["paper"] is True and "never holds a wallet key" in data["note"] and data["signals"]
    assert client.get("/api/polymarket/bot/signals?bankroll=-5").status_code == 422

    ok = client.post("/api/polymarket/bot/review", json={"positions": [position()]})
    assert ok.status_code == 200 and ok.json()["decisions"][0]["id"] == "p1"
    for bad in ({"positions": [position(token_id="abc")]}, {"positions": [position(token="MAYBE")]},
                {"positions": [position(entry=1.5)]}, {"positions": [position()] * 21}, {"positions": [], "extra": 1}):
        assert client.post("/api/polymarket/bot/review", json=bad).status_code == 422

    def down(*a, **k):
        raise pm.PolymarketError("Polymarket is unreachable right now")
    monkeypatch.setattr(bot, "_ladders", down)
    response = client.get("/api/polymarket/bot/signals")
    assert response.status_code == 503 and "pauses rather than guess" in response.json()["detail"]


# --- feed status lights --------------------------------------------------------------
def test_feed_status_reports_worst_state_and_never_500s(monkeypatch):
    def boom():
        raise ConnectionError("secret-host.internal:443")
    monkeypatch.setattr(feed_status, "CHECKS", [("stocks", "Stocks", lambda: (True, "SPY 1")),
                                                 ("odds", "Odds", boom),
                                                 ("ai", "AI", lambda: (True, "ok"))])
    data = client.get("/api/status/feeds")
    assert data.status_code == 200 and data.headers["cache-control"] == "no-store"
    body = data.json()
    assert body["overall"] == "down"
    states = {f["key"]: f for f in body["feeds"]}
    assert states["stocks"]["state"] == "ok" and states["odds"]["state"] == "down"
    assert states["odds"]["detail"] == "unreachable (ConnectionError)"  # no hostnames or secrets leak
    assert "secret-host" not in json.dumps(body)


def test_feed_status_flags_slow_market_feeds(monkeypatch):
    monkeypatch.setattr(feed_status, "SLOW_MS", -1)
    monkeypatch.setattr(feed_status, "CHECKS", [("crypto", "Crypto", lambda: (True, "x")), ("ai", "AI", lambda: (True, "y"))])
    body = feed_status.snapshot(force=True)
    assert body["overall"] == "slow" and [f["state"] for f in body["feeds"]] == ["slow", "ok"]


def test_unconfigured_services_show_setup_not_an_outage(monkeypatch):
    monkeypatch.setattr(feed_status, "CHECKS", [("stocks", "Stocks", lambda: (True, "x")),
                                                 ("accounts", "Accounts", lambda: (None, "not set up"))])
    body = feed_status.snapshot(force=True)
    assert body["overall"] == "setup" and body["feeds"][1]["state"] == "setup"

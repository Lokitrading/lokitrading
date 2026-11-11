"""Server relays added for engine v3: central-bank newswire, Kalshi/Polymarket
cross-venue check, telemetry and the signal relay."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from loki.api import engine_feeds as ef
from loki.api.rate_limit import rule_for
from loki.api.server import CONTENT_SECURITY_POLICY, app

client = TestClient(app)

RSS = b"""\xef\xbb\xbf<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bank Rate increased to 4%</title><link>https://www.bankofengland.co.uk/x</link><description>Monetary Policy Summary</description><pubDate>Thu, 05 Nov 2026 12:00:00 Z</pubDate></item>
<item><title>Board announces enforcement action</title><link>https://www.federalreserve.gov/y</link><pubDate>Thu, 05 Nov 2026 16:00:00 GMT</pubDate></item>
<item><title>Federal Reserve cuts interest rates by 25 basis points</title><link>javascript:alert(1)</link><pubDate>Wed, 04 Nov 2026 18:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_feed_parsing_scores_only_policy_items_and_keeps_the_evidence():
    items = ef.parse_feed("BoE", RSS)
    assert [i["policy"] for i in items] == [True, False, True]
    hike, enforcement, cut = items
    assert hike["tone"] > 0 and hike["matched"]
    assert enforcement["tone"] == 0 and enforcement["matched"] == []
    assert cut["tone"] < 0 and cut["url"] is None  # only https links survive
    now = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()
    tone, n = ef.blend_tone(items, now=now)
    assert n == 2 and -1 < tone < 1


def test_one_headline_cannot_swing_the_tone_all_the_way():
    one = [{"policy": True, "matched": ["+raised"], "tone": 1.0, "published": "2026-11-05T12:00:00+00:00"}]
    tone, n = ef.blend_tone(one, now=datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp())
    assert n == 1 and tone == 0.5


def test_feed_parser_refuses_entity_declarations():
    assert ef.parse_feed("x", b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>') == []


def test_newswire_endpoint(monkeypatch):
    monkeypatch.setattr(ef, "_fetch", lambda url, timeout=8.0: RSS)
    ef._cache.clear()
    res = client.get("/api/newswire")
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 9 and "method" in body and body["errors"] == {}


def test_title_matching_respects_numbers_and_fees():
    assert ef.similarity("Will Bitcoin hit $150k in 2026?", "Bitcoin above 150k in 2026") > 0.3
    assert ef.similarity("Fed cuts rates in December", "Fed cuts rates in December") == 1.0
    assert ef.similarity("BTC above 100000", "BTC above 120000") < 0.5  # different thresholds are different questions
    assert ef.kalshi_fee(0.5) == 0.02 and ef.kalshi_fee(0.99) == 0.01 and ef.kalshi_fee(0.0) == 0.0


def test_cross_venue_pairs_price_both_directions():
    kalshi = [{"ticker": "K1", "title": "Will the Fed cut rates in December 2026", "yes_ask": 0.40, "no_ask": 0.58, "close": None, "rules": "", "url": "https://kalshi.com/markets/k"}]
    poly = [{"id": "1", "title": "Fed cut rates in December 2026?", "yes_ask": 0.45, "no_ask": 0.52, "close": None, "rules": "", "url": "https://polymarket.com/event/x"},
            {"id": "2", "title": "Taylor Swift album of the year", "yes_ask": 0.3, "no_ask": 0.7, "close": None, "rules": "", "url": "https://polymarket.com/event/y"}]
    pairs = ef.match_pairs(kalshi, poly)
    assert len(pairs) == 1
    p = pairs[0]
    # YES on Kalshi (0.40 + fee 0.02) + NO on Polymarket (0.52) = 0.94 beats the other direction (0.45 + 0.58 + 0.02).
    assert p["trade"] == "YES on Kalshi + NO on Polymarket" and p["cost"] == 0.94 and p["arb"] is True


def test_telemetry_reports_instance_health_without_secrets():
    res = client.get("/api/status/telemetry")
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    body = res.json()
    assert set(body) >= {"uptime_s", "rss_mb", "cpu_s", "region", "python"}
    assert not any("secret" in k or "key" in k for k in body)


def test_signal_relay_only_reaches_the_three_webhook_hosts(monkeypatch):
    sent = []

    class Res:
        status_code = 204

    monkeypatch.setattr(ef.requests, "post", lambda url, **kw: sent.append((url, kw["json"])) or Res())
    for url in ("https://evil.example.com/hook", "http://discord.com/api/webhooks/123456789012/" + "a" * 60,
                "https://discord.com.evil.io/api/webhooks/123456789012/" + "a" * 60, "https://169.254.169.254/latest/meta-data"):
        assert client.post("/api/signals/relay", json={"url": url, "text": "hi"}).status_code == 422
    discord = "https://discord.com/api/webhooks/123456789012/" + "a" * 60
    assert client.post("/api/signals/relay", json={"url": discord, "text": "BUY BTC"}).status_code == 200
    assert sent[-1][1] == {"content": "BUY BTC", "allowed_mentions": {"parse": []}}
    tg = "https://api.telegram.org/bot123456:" + "A" * 35 + "/sendMessage"
    assert client.post("/api/signals/relay", json={"url": tg, "text": "x"}).status_code == 422  # needs a chat id
    assert client.post("/api/signals/relay", json={"url": tg, "text": "x", "chat_id": "-100123"}).status_code == 200
    assert sent[-1][1]["chat_id"] == "-100123"


def test_new_endpoints_are_rate_limited_and_device_hosts_allowed():
    assert rule_for("GET", "/api/xarb") and rule_for("GET", "/api/newswire") and rule_for("POST", "/api/signals/relay")
    assert rule_for("GET", "/api/status/telemetry")[1] == 30
    assert "https://api.exchange.coinbase.com" in CONTENT_SECURITY_POLICY
    assert "https://data-api.polymarket.com" in CONTENT_SECURITY_POLICY


def test_workspace_has_command_center_and_new_scripts():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    for needle in ('id="command"', 'data-view="command"', "command_center.js", "strategy_nl.js", "consensus.js", "mcrisk.js", "execq.js", "evolve.js", "pm_lab.js", 'id="pm-mm"', 'id="redteam"', "jury.js", "redteam.js", "fillsim.js", "redteam_view.js"):
        assert needle in html
    # Engine modules load before the views that use them.
    assert html.index("engine/consensus.js") < html.index("js/trade.js") < html.index("command_center.js")
    assert html.index("engine/quant.js") < html.index("engine/fillsim.js") < html.index("engine/redteam.js") < html.index("engine/jury.js") < html.index("redteam_view.js")


def test_share_class_tickers_map_to_yahoo_and_exchange_suffixes_do_not():
    from loki_v8.data import normalize_symbol, yahoo_ticker
    assert yahoo_ticker("brk.b") == "BRK-B" and yahoo_ticker("BF/B") == "BF-B" and yahoo_ticker("HEI.A") == "HEI-A"
    for untouched in ("VOD.L", "SHOP.TO", "RELIANCE.NS", "SPY", "GC=F", "^GSPC", "BTC-USD", "BRK-B"):
        assert yahoo_ticker(untouched) == untouched
    assert normalize_symbol("BRK.B") == "BRK-B" and normalize_symbol("BTCUSDT") == "BTC-USD"


def test_unknown_symbol_is_a_404_with_a_hint_not_a_retry_later(monkeypatch):
    import pandas as pd
    from loki.api import ai_paper

    class Empty:
        def fetch_history(self, *a, **k):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    class Pipe:
        provider = Empty()

    class Cfg:
        period = "2y"

    monkeypatch.setattr(ai_paper, "_get_pipeline", lambda s: (Pipe(), Cfg(), {}))
    res = client.get("/api/fusion/ai/bars?symbol=NOPE&timeframe=1d&limit=60")
    assert res.status_code == 404 and "Check the symbol" in res.json()["detail"]

    class Down:
        def fetch_history(self, *a, **k):
            raise ConnectionError("provider down")

    Pipe.provider = Down()
    res = client.get("/api/fusion/ai/bars?symbol=SPY&timeframe=1d&limit=60")
    assert res.status_code == 503


FOMC_HTML = """<h4>2026 FOMC Meetings</h4>
<div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">27-28</div>
<div class="fomc-meeting__month"><strong>March</strong></div><div class="fomc-meeting__date">17-18*</div>
<h4>2025 FOMC Meetings</h4>
<div class="fomc-meeting__month"><strong>Apr/May</strong></div><div class="fomc-meeting__date">30-1</div>"""


def test_fomc_calendar_parses_decision_days_at_2pm_eastern():
    events = ef.parse_fomc(FOMC_HTML)
    times = [e["time"] for e in events]
    assert "2026-01-28T14:00:00-05:00" in times and "2026-03-18T14:00:00-04:00" in times  # DST handled
    assert any(e["projections"] for e in events) and all(not e["estimated"] for e in events)
    assert "2025-05-01T14:00:00-04:00" in times  # meetings spanning two months use the later month


def test_jobs_report_estimates_skip_holidays():
    from datetime import datetime, timezone
    jan = ef.jobs_reports(datetime(2027, 1, 1, tzinfo=timezone.utc), 0)[0]
    assert jan["time"].startswith("2027-01-08T08:30") and jan["estimated"] is True
    jul = ef.jobs_reports(datetime(2025, 7, 1, tzinfo=timezone.utc), 0)[0]
    assert jul["time"].startswith("2025-07-03")


def test_calendar_endpoint(monkeypatch):
    monkeypatch.setattr(ef, "_fetch", lambda url, timeout=8.0: FOMC_HTML.encode())
    ef._cache.pop("calendar", None)
    res = client.get("/api/calendar/macro")
    assert res.status_code == 200 and "CPI" in res.json()["note"]
    assert rule_for("GET", "/api/calendar/macro")


def test_retired_desk_scenario_and_research_endpoints_are_gone():
    for method, path in (("post", "/api/desk/run"), ("post", "/api/fusion/council"), ("post", "/api/fusion/scenario"),
                         ("post", "/api/fusion/narrative"), ("get", "/api/fusion/native-mirofish/reports"), ("get", "/api/fusion/mirofish/status")):
        res = client.post(path, json={}) if method == "post" else client.get(path)
        assert res.status_code in (404, 405), path


def test_3d_map_has_glow_pipeline_a_safe_fallback_and_the_day_trader_node():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "engine" / "execmap.js").read_text(encoding="utf-8")
    for needle in ("WebGLRenderTarget", "tBloom", "post = null", "id: 'daytrader'", "daytrader:trade", "prefers-reduced-motion", "are decoration"):
        assert needle in js
    assert "quality governor" in js and "43758.5453" not in js  # slow devices step down; the flickering grain is gone
    assert "fit = Math.max(1" in js  # narrow stages pull the camera back
    css = (root / "static" / "css" / "engine.css").read_text(encoding="utf-8")
    assert "align-self: start; height: min(74vh, 720px)" in css  # the stage no longer stretches to the tables' height
    trader = (root / "static" / "js" / "day_trader.js").read_text(encoding="utf-8")
    assert "emit('daytrader:trade'" in trader and "emit('daytrader:plan'" in trader

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
    for needle in ('id="command"', 'data-view="command"', "command_center.js", "strategy_nl.js", "consensus.js", "mcrisk.js", "execq.js", "evolve.js", "pm_lab.js", 'id="pm-mm"'):
        assert needle in html
    # Engine modules load before the views that use them.
    assert html.index("engine/consensus.js") < html.index("js/trade.js") < html.index("command_center.js")

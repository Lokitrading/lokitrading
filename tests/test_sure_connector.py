"""Sure connector: safe addresses, read-only relay, and Loki's AI beside your real holdings."""
from __future__ import annotations

import io
import json
import socket

import pytest
from fastapi.testclient import TestClient

from loki.api import sure_connector as sc
from loki.api.server import app

client = TestClient(app)
KEY = "sure-test-key-123"


def resolve_to(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", (ip, 0))])


class TestAddresses:
    def test_public_https_is_accepted_and_trimmed_to_its_root(self, monkeypatch):
        resolve_to(monkeypatch, "93.184.216.34")
        assert sc.base_url("https://sure.example.com/app/dashboard?x=1") == "https://sure.example.com"
        assert sc.base_url("sure.example.com:8443") == "https://sure.example.com:8443"

    def test_plain_http_to_the_internet_is_refused(self, monkeypatch):
        resolve_to(monkeypatch, "93.184.216.34")
        with pytest.raises(sc.SureError, match="https"):
            sc.base_url("http://sure.example.com")

    @pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.20", "::1"])
    def test_private_addresses_are_refused_on_the_website(self, monkeypatch, ip):
        monkeypatch.delenv("LOKI_DESKTOP", raising=False)
        monkeypatch.delenv("LOKI_SURE_ALLOW_PRIVATE", raising=False)
        resolve_to(monkeypatch, ip)
        with pytest.raises(sc.SureError, match="desktop app"):
            sc.base_url("https://my-sure.local")

    def test_the_desktop_app_may_reach_a_sure_on_this_computer(self, monkeypatch):
        monkeypatch.setenv("LOKI_DESKTOP", "1")
        resolve_to(monkeypatch, "127.0.0.1")
        assert sc.base_url("http://localhost:3000") == "http://localhost:3000"

    def test_cloud_metadata_and_embedded_credentials_are_always_refused(self, monkeypatch):
        monkeypatch.setenv("LOKI_DESKTOP", "1")
        resolve_to(monkeypatch, "169.254.169.254")
        with pytest.raises(sc.SureError):
            sc.base_url("http://metadata.internal")
        resolve_to(monkeypatch, "93.184.216.34")
        with pytest.raises(sc.SureError):
            sc.base_url("https://user:pass@sure.example.com")
        with pytest.raises(sc.SureError):
            sc.base_url("ftp://sure.example.com")


SHEET = {"currency": "USD", "net_worth": {"amount": "152000.50"}, "assets": {"amount": "180000"}, "liabilities": {"amount": "27999.50"}}
ACCOUNTS = {"accounts": [
    {"id": "a1", "name": "Checking", "balance_cents": 1500000, "currency": "USD", "classification": "asset", "account_type": "depository", "status": "active"},
    {"id": "a2", "name": "Brokerage", "balance_cents": 16500000, "currency": "USD", "classification": "asset", "account_type": "investment", "status": "active"},
    {"id": "a3", "name": "Card", "balance_cents": 2799950, "currency": "USD", "classification": "liability", "account_type": "credit_card", "status": "active"},
], "pagination": {"page": 1, "total_pages": 1}}
HOLDINGS = {"holdings": [
    {"id": "h1", "date": "2026-11-17", "qty": "100", "price": "500", "amount": "50000", "currency": "USD", "account": {"id": "a2", "name": "Brokerage"}, "security": {"id": "s1", "ticker": "SPY", "name": "S&P 500 ETF"}},
    {"id": "h1b", "date": "2026-11-18", "qty": "100", "price": "600", "amount": "60000", "currency": "USD", "account": {"id": "a2", "name": "Brokerage"}, "security": {"id": "s1", "ticker": "SPY", "name": "S&P 500 ETF"}},
    {"id": "h2", "date": "2026-11-18", "qty": "1", "price": "90000", "amount": "90000", "currency": "USD", "account": {"id": "a2", "name": "Brokerage"}, "security": {"id": "s2", "ticker": "BTC", "name": "Bitcoin"}},
    {"id": "h3", "date": "2026-11-18", "qty": "10", "price": "1500", "amount": "15000", "currency": "USD", "account": {"id": "a2", "name": "Brokerage"}, "security": {"id": "s3", "ticker": "ACME", "name": "Acme Corp"}},
], "pagination": {"page": 1, "total_pages": 1}}
POSITIONS = [{"symbol": "SPY", "score": 42, "regime": "BULL", "weight": 0.12}, {"symbol": "BTC-USD", "score": -30, "regime": "BEAR", "weight": 0}]


class FakeReply:
    def __init__(self, status, payload):
        self.status_code = status
        self.raw = io.BytesIO(json.dumps(payload).encode())
        self.raw.read = (lambda f: lambda n=-1, decode_content=True: f(n))(self.raw.read)

    def close(self):
        pass


@pytest.fixture
def fake_sure(monkeypatch):
    resolve_to(monkeypatch, "93.184.216.34")
    seen = []

    def get(url, params=None, timeout=None, allow_redirects=True, stream=False, headers=None):
        seen.append({"url": url, "params": params, "redirects": allow_redirects, "headers": headers})
        if headers.get("X-Api-Key") != KEY:
            return FakeReply(401, {"error": "unauthorized"})
        path = url.split("sure.example.com", 1)[1]
        return FakeReply(200, {"/api/v1/balance_sheet": SHEET, "/api/v1/accounts": ACCOUNTS, "/api/v1/holdings": HOLDINGS}[path])

    monkeypatch.setattr(sc.requests, "get", get)
    monkeypatch.setattr(sc, "_loki_positions", lambda: POSITIONS)
    return seen


class TestOverview:
    def test_reads_sure_and_puts_lokis_ai_beside_each_holding(self, fake_sure):
        r = client.get("/api/sure/overview", headers={"X-Sure-Url": "https://sure.example.com", "X-Sure-Key": KEY})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["net_worth"] == 152000.5 and d["liabilities"] == 27999.5 and d["server"] == "sure.example.com"
        by = {h["ticker"]: h for h in d["holdings"]}
        assert by["SPY"]["value"] == 60000  # the newest day of each holding, not the older one
        assert by["SPY"]["loki"] == {"symbol": "SPY", "trend_score": 42.0, "regime": "BULL", "ai_holds": True, "ai_weight_pct": 12.0, "trend": "uptrend"}
        assert by["BTC"]["loki"]["symbol"] == "BTC-USD" and by["BTC"]["loki"]["trend"] == "downtrend"
        assert by["ACME"]["loki"] is None
        assert d["invested"] == 165000
        assert any("largest holding, BTC" in f for f in d["facts"]) and any("Crypto is 55%" in f for f in d["facts"])
        assert any("covers 2 of your 3 holdings; 1 of those are in a downtrend" in f for f in d["facts"])

    def test_read_only_no_redirects_and_the_key_never_comes_back(self, fake_sure):
        r = client.get("/api/sure/overview", headers={"X-Sure-Url": "https://sure.example.com", "X-Sure-Key": KEY})
        assert KEY not in r.text and r.headers["cache-control"] == "private, no-store"
        assert all(c["redirects"] is False for c in fake_sure)
        assert {c["url"].rsplit("/", 1)[1] for c in fake_sure} == {"balance_sheet", "accounts", "holdings"}

    def test_a_wrong_key_is_explained(self, fake_sure):
        r = client.get("/api/sure/overview", headers={"X-Sure-Url": "https://sure.example.com", "X-Sure-Key": "nope"})
        assert r.status_code == 401 and "API key" in r.json()["detail"]

    def test_missing_address_or_key_is_explained(self, fake_sure):
        assert client.get("/api/sure/overview", headers={"X-Sure-Key": KEY}).status_code == 400
        assert client.get("/api/sure/overview", headers={"X-Sure-Url": "https://sure.example.com"}).status_code == 400


def test_the_workspace_has_the_your_money_view():
    html = client.get("/").text
    assert 'id="my-money"' in html and 'href="#my-money"' in html and "/static/js/sure_view.js" in html


def test_a_slow_ai_never_holds_up_your_money(monkeypatch):
    import time

    from loki.api import ai_paper

    monkeypatch.setattr(ai_paper, "ai_portfolio", lambda response: time.sleep(3) or {"positions": POSITIONS})
    started = time.monotonic()
    assert sc._loki_positions(wait_s=0.5) == []
    assert time.monotonic() - started < 2
    monkeypatch.setattr(ai_paper, "ai_portfolio", lambda response: {"positions": POSITIONS})
    assert sc._loki_positions(wait_s=2) == POSITIONS

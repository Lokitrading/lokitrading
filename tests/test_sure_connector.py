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


@pytest.mark.parametrize("text,expected", [("$60,000.00", 60000), ("A$1,234.50", 1234.5), ("€1.234,56", 1234.56),
                                           ("-$12.00", -12), ("177200.0", 177200), (16500000, 16500000),
                                           ("1.234.567", 1234567), ("$1,234", 1234), ("—", None), (None, None)])
def test_money_in_any_currency_format(text, expected):
    """Real Sure sends formatted amounts ("$60,000.00"); every currency's format must parse."""
    assert sc._num(text) == expected


def test_the_local_sure_status_is_only_for_the_desktop_app(monkeypatch):
    monkeypatch.delenv("LOKI_DESKTOP", raising=False)
    assert client.get("/api/sure/local").json() == {"desktop": False}
    monkeypatch.setenv("LOKI_DESKTOP", "1")
    monkeypatch.setenv("LOKI_SURE_LOCAL", "no-docker")
    monkeypatch.setenv("LOKI_SURE_LOCAL_URL", "http://127.0.0.1:3000")
    assert client.get("/api/sure/local").json() == {"desktop": True, "url": "http://127.0.0.1:3000", "state": "no-docker"}
    monkeypatch.setenv("LOKI_SURE_LOCAL", "starting")
    monkeypatch.setattr(sc.requests, "get", lambda *a, **k: type("R", (), {"status_code": 200})())
    assert client.get("/api/sure/local").json()["state"] == "running"


class TestSureOnThisComputer:
    """The desktop app runs Sure beside Loki: settings written once, never shared, Docker optional."""

    @pytest.fixture
    def local(self):
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "desktop"))
        try:
            import sure_local
        finally:
            sys.path.remove(str(root / "desktop"))
        return sure_local, root

    def test_secrets_are_created_once_on_this_computer_and_kept(self, local, tmp_path):
        sure_local, root = local
        folder = sure_local.prepare(tmp_path, root)
        first = (folder / ".env").read_text()
        for key in sure_local.ENV_KEYS:
            assert f"{key}=" in first
        assert (folder / "compose.yml").read_text() == (root / "desktop/sure/compose.yml").read_text()
        assert sure_local.prepare(tmp_path, root) == folder and (folder / ".env").read_text() == first
        other = sure_local.prepare(tmp_path / "other", root)
        assert (other / ".env").read_text() != first  # every computer gets its own

    def test_sure_only_listens_on_this_computer(self, local):
        _, root = local
        compose = (root / "desktop/sure/compose.yml").read_text()
        assert '"127.0.0.1:${SURE_PORT:-3000}:3000"' in compose and "ghcr.io/we-promise/sure" in compose
        assert "SECRET_KEY_BASE: ${SECRET_KEY_BASE:?missing}" in compose  # never a shared default secret

    def test_without_docker_loki_says_so_and_carries_on(self, local, tmp_path, monkeypatch):
        sure_local, root = local
        monkeypatch.setattr(sure_local, "answering", lambda *a, **k: False)
        monkeypatch.setattr(sure_local, "docker_ready", lambda: None)
        assert sure_local.start(tmp_path, root) == "no-docker"
        monkeypatch.setenv("LOKI_SURE", "0")
        assert sure_local.start(tmp_path, root) == "off"

    def test_with_docker_it_starts_the_official_image(self, local, tmp_path, monkeypatch):
        sure_local, root = local
        calls = []
        monkeypatch.setattr(sure_local, "answering", lambda *a, **k: False)
        monkeypatch.setattr(sure_local, "docker_ready", lambda: "docker")
        monkeypatch.setattr(sure_local, "_run", lambda args, cwd=None, timeout=30: calls.append((args, cwd)) or
                            type("D", (), {"returncode": 0, "stdout": "", "stderr": ""})())
        assert sure_local.start(tmp_path, root) == "starting"
        args, cwd = calls[0]
        assert args[:4] == ["docker", "compose", "-p", "loki-sure"] and args[-2:] == ["up", "-d"] and cwd == tmp_path / "sure"

"""Protect private native research while keeping pure paper tools public."""

from datetime import datetime, timedelta, timezone
import math

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import core.fusion.real_account as real_account
from loki.api.fusion import router


@pytest.fixture
def app(monkeypatch):
    for key in ("LOKI_FUSION_LOCAL", "VERCEL"):
        monkeypatch.delenv(key, raising=False)
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture
def local_client(app, monkeypatch):
    monkeypatch.setenv("LOKI_FUSION_LOCAL", "1")
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 12345)) as client:
        yield client


def native_headers(client):
    response = client.get("/api/fusion/capabilities")
    assert response.status_code == 200
    return {"X-Loki-Local-Token": response.json()["local_token"]}


@pytest.mark.parametrize(
    "path",
    ["/api/fusion/real/status", "/api/fusion/real/orders"],
)
def test_private_native_reads_require_local_session_token(local_client, path):
    assert local_client.get(path).status_code == 403


def test_remote_clients_never_receive_local_token_or_native_data(app, monkeypatch):
    monkeypatch.setenv("LOKI_FUSION_LOCAL", "1")
    with TestClient(
        app, base_url="http://127.0.0.1:8765", client=("203.0.113.10", 12345)
    ) as client:
        capabilities = client.get("/api/fusion/capabilities").json()
        assert capabilities["local_actions_enabled"] is False
        assert "local_token" not in capabilities
        assert client.get("/api/fusion/real/status").status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "public.example"},
        {"Host": "localhost:invalid"},
        {"Host": "127.0.0.1:0"},
        {"Host": "user@127.0.0.1:8765"},
        {"Origin": "https://external.example"},
        {"Origin": "http://127.0.0.1:8766"},
        {"Origin": "http://127.0.0.1:8765/path"},
        {"Origin": "null"},
        {"X-Forwarded-For": "203.0.113.1"},
        {"X-Forwarded-Host": "public.example"},
        {"X-Forwarded-Proto": "https"},
        {"Forwarded": "for=203.0.113.1"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_native_token_cannot_be_obtained_via_rebinding_proxy_or_foreign_origin(
    local_client, headers
):
    response = local_client.get("/api/fusion/capabilities", headers=headers)
    assert response.status_code == 200
    assert "local_token" not in response.json()
    assert response.json()["local_actions_enabled"] is False


def test_local_launcher_flag_and_vercel_disable_token_issuance(local_client, monkeypatch):
    monkeypatch.delenv("LOKI_FUSION_LOCAL")
    assert "local_token" not in local_client.get("/api/fusion/capabilities").json()
    monkeypatch.setenv("LOKI_FUSION_LOCAL", "1")
    monkeypatch.setenv("VERCEL", "1")
    assert "local_token" not in local_client.get("/api/fusion/capabilities").json()


def test_same_origin_default_port_is_compared_correctly(app, monkeypatch):
    monkeypatch.setenv("LOKI_FUSION_LOCAL", "1")
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        response = client.get("/api/fusion/capabilities", headers={"Origin": "http://localhost:80"})
        assert response.json()["local_actions_enabled"] is True


def test_private_data_and_tokens_are_not_cacheable(local_client, monkeypatch):
    capabilities = local_client.get("/api/fusion/capabilities")
    assert "no-store" in capabilities.headers["cache-control"]
    headers = {"X-Loki-Local-Token": capabilities.json()["local_token"]}
    monkeypatch.setattr(real_account, "status", lambda environ=None: {"state": "private research"})
    response = local_client.get("/api/fusion/real/status", headers=headers)
    assert response.status_code == 200
    assert response.json()["state"] == "private research"
    assert "private" in response.headers["cache-control"]
    assert "no-store" in response.headers["cache-control"]


def test_valid_token_is_still_rejected_for_foreign_origin(local_client, monkeypatch):
    headers = native_headers(local_client)
    headers["Origin"] = "http://evil.example"
    monkeypatch.setattr(real_account, "status", lambda environ=None: pytest.fail("private data must not be read"))
    assert local_client.get("/api/fusion/real/status", headers=headers).status_code == 403


def test_valid_token_cannot_be_reused_from_a_remote_client(local_client, app):
    headers = native_headers(local_client)
    with TestClient(
        app, base_url="http://127.0.0.1:8765", client=("203.0.113.10", 12345)
    ) as remote:
        assert remote.get("/api/fusion/real/status", headers=headers).status_code == 403


def test_public_paper_research_tools_remain_available_without_native_token(app):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars, previous = [], 100.0
    for index in range(90):
        close = 100 * math.exp(0.0009 * index + 0.06 * math.sin(index / 8))
        bars.append(
            {
                "timestamp": (start + timedelta(days=index)).isoformat(),
                "open": previous,
                "high": max(previous, close) * 1.007,
                "low": min(previous, close) * 0.993,
                "close": close,
                "volume": 100_000.0,
            }
        )
        previous = close
    with TestClient(app, client=("203.0.113.10", 12345)) as client:
        assert client.get("/api/fusion/portfolio").status_code == 200
        assert client.post("/api/fusion/portfolio", json={"action": "snapshot"}).status_code == 200
        quant = client.post("/api/fusion/quant", json={"bars": bars})
        assert quant.status_code == 200
        assert "replay" in quant.json() and "analysis" in quant.json()

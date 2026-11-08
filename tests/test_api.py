"""Contract tests for Loki's paper-only web surface."""
from dataclasses import dataclass

from fastapi.testclient import TestClient

import loki.api.server as server

client = TestClient(server.app)


def test_health_endpoint_identifies_the_paper_only_mode():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "loki"

def test_health_names_missing_deployment_settings(monkeypatch):
    from loki.api import site_access

    monkeypatch.setenv("VERCEL", "1")
    for name in ("LOKI_GATE_SECRET", "LOKI_BROKER_ENCRYPTION_KEY", "SUPABASE_URL", *site_access.SUPABASE_KEY_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    text = " ".join(body["configuration_warnings"])
    assert "LOKI_GATE_SECRET" in text and "SUPABASE_URL" in text and "LOKI_BROKER_ENCRYPTION_KEY" in text

    monkeypatch.setenv("LOKI_GATE_SECRET", "x" * 40)
    monkeypatch.setenv("LOKI_BROKER_ENCRYPTION_KEY", "k")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv(site_access.SUPABASE_KEY_ENV_NAMES[0], "service-key")
    healthy = client.get("/health").json()
    assert healthy["status"] == "ok" and "configuration_warnings" not in healthy
    # values are never echoed
    assert "x" * 40 not in str(body) + str(healthy)

"""Cookies keep working across serverless instances even before an operator sets secrets.

On Vercel every request can land on a different instance. A random
per-instance signing key bounced visitors back to the access-code page on
almost every navigation, which also broke Talk to Loki and the sign-in page
behind it.
"""

import hashlib
import hmac
import time

import pytest

from loki.api import site_access


@pytest.fixture
def public_no_secrets(monkeypatch):
    for name in ("LOKI_GATE_SECRET", "LOKI_SITE_CODE", *site_access.SUPABASE_KEY_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "lokitrading.vercel.app")
    return monkeypatch


def new_instance(monkeypatch):
    """A cold start: every per-process random key is drawn again."""
    monkeypatch.setattr(site_access, "_PROCESS_SECRET", hashlib.sha256(str(time.perf_counter_ns()).encode()).digest())


def test_gate_and_terms_cookies_survive_another_instance(public_no_secrets):
    gate, terms = site_access.issue_gate_cookie_value(), site_access.issue_terms_cookie_value()
    new_instance(public_no_secrets)
    assert site_access.verify_gate_cookie_value(gate) is True
    assert site_access.read_terms_cookie_value(terms) == site_access.TERMS_VERSION


def test_changing_the_access_code_relocks_every_browser(public_no_secrets):
    gate = site_access.issue_gate_cookie_value()
    public_no_secrets.setenv("LOKI_SITE_CODE", "a brand new code")
    assert site_access.verify_gate_cookie_value(gate) is False


def test_gate_key_is_not_the_public_dev_constant(public_no_secrets):
    payload = str(int(time.time()) + 3600)
    dev = hashlib.sha256(site_access._COOKIE_SALT + b"loki-local-dev-gate").digest()
    forged = payload + "." + hmac.new(dev, payload.encode(), hashlib.sha256).hexdigest()
    assert site_access.verify_gate_cookie_value(forged) is False


def test_account_sessions_never_use_the_code_derived_key(public_no_secrets):
    payload = "eyJ1aWQiOiJ4In0"
    forged = payload + "." + hmac.new(site_access.gate_secret(), payload.encode(), hashlib.sha256).hexdigest()
    assert site_access.read_session_cookie_value(forged) is None


def test_configured_secret_still_wins(public_no_secrets):
    public_no_secrets.setenv("LOKI_GATE_SECRET", "operator-secret-value-0123456789abcdef")
    gate = site_access.issue_gate_cookie_value()
    public_no_secrets.setenv("LOKI_GATE_SECRET", "a-different-operator-secret-0123456789")
    assert site_access.verify_gate_cookie_value(gate) is False

"""Memecoin bot on Alpaca paper crypto: order logic and every guard, against a fake Alpaca."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from loki.api import alpaca_memecoin as am

NOW = datetime(2026, 11, 14, 13, 0, tzinfo=timezone.utc)
KEYS = {"key_id": "PKTESTKEY123456", "secret_key": "s" * 40, "confirm": "trade memecoins with paper money"}


def holdings(**on):
    return [{"coin": c, "holding": c in on, "weight": on.get(c, 0.0), "as_of": "2026-11-14 12:00:00+00:00"} for c in am.mc.COINS]


class FakeAlpaca:
    def __init__(self, held=None, cash=20_000.0, spread=0.3, quote_age=10, open_orders=(), refuse=None):
        self.held, self.cash, self.spread, self.quote_age = dict(held or {}), cash, spread, quote_age
        self.open_orders, self.refuse, self.sent, self.ids = list(open_orders), refuse, [], set()

    def __call__(self, method, path, keys, body=None):
        if path == "/v2/account":
            return {"equity": "25000", "last_equity": "25000", "buying_power": "80000", "non_marginable_buying_power": str(self.cash)}
        if path == "/v2/clock":
            return {"is_open": False, "timestamp": NOW.isoformat()}
        if path == "/v2/positions":
            return [{"symbol": "AMZN", "qty": "4", "asset_class": "us_equity"}] + [{"symbol": f"{c}USD", "qty": str(q), "asset_class": "crypto"} for c, q in self.held.items()]
        if path.startswith("/v2/orders?status=open"):
            return [{"symbol": f"{c}/USD"} for c in self.open_orders]
        if method == "DELETE" and path.startswith("/v2/positions/"):
            coin = path.rsplit("/", 1)[1][:-3]
            self.sent.append(("sell", coin, self.held.pop(coin, 0)))
            return {"id": "00000000-0000-0000-0000-000000000001", "symbol": f"{coin}/USD", "status": "accepted"}
        if method == "POST" and path == "/v2/orders":
            if self.refuse:
                raise HTTPException(422, self.refuse)
            if body["client_order_id"] in self.ids:
                raise HTTPException(422, "Alpaca refused this order: client_order_id must be unique")
            assert body["type"] == "market" and body["time_in_force"] == "ioc" and body["side"] == "buy" and "/" in body["symbol"]
            self.ids.add(body["client_order_id"])
            self.sent.append(("buy", body["symbol"].split("/")[0], body["notional"]))
            return {"id": "00000000-0000-0000-0000-000000000002", "symbol": body["symbol"], "status": "accepted"}
        raise AssertionError(f"unexpected {method} {path}")

    def data(self, method, path, keys, body=None, base=None):
        assert path.startswith("/v1beta3/crypto/us/latest/quotes?symbols=")
        mid = 0.0000043  # memecoin prices sit far below a cent: they must not be rounded away
        half = mid * self.spread / 200
        stamp = (NOW - timedelta(seconds=self.quote_age)).isoformat().replace("+00:00", "Z")
        return {"quotes": {f"{c}/USD": {"bp": mid - half, "ap": mid + half, "t": stamp} for c in am.mc.COINS}}


@pytest.fixture
def run(monkeypatch):
    def go(alpaca, plan_holdings, **flags):
        from loki.api import memecoin_api
        monkeypatch.setattr(memecoin_api, "_cached_plan", lambda: {"holdings": plan_holdings, "label": "breakout 4h entry 60 exit 30"})
        monkeypatch.setattr(am, "_paper_exchange", alpaca)
        monkeypatch.setattr(am, "_exchange", alpaca.data)
        return am._check(am.AlpacaMemecoinCheck(**KEYS, **flags))
    return go


def test_buys_what_the_rule_turns_on_within_the_allocation(run):
    alpaca = FakeAlpaca()
    out = run(alpaca, holdings(DOGE=0.1, PEPE=0.05), allocation_pct=10)
    assert sorted(alpaca.sent) == [("buy", "DOGE", "250.00"), ("buy", "PEPE", "125.00")]  # 25,000 x 10% x weight
    assert all(a["status"] == "sent" and a["receipt"]["policy"] == am.POLICY_HASH for a in out["actions"])


def test_sells_in_full_when_the_rule_turns_off_and_never_touches_stocks(run):
    alpaca = FakeAlpaca(held={"WIF": 120.5})
    run(alpaca, holdings())
    assert alpaca.sent == [("sell", "WIF", 120.5)]  # AMZN untouched


def test_refuses_to_start_on_an_account_holding_its_coins(run):
    with pytest.raises(HTTPException) as caught:
        run(FakeAlpaca(held={"DOGE": 10}), holdings(), start=True)
    assert caught.value.status_code == 409 and "DOGE" in caught.value.detail


def test_stop_sells_everything_and_buys_nothing(run):
    alpaca = FakeAlpaca(held={"DOGE": 10, "BONK": 99})
    out = run(alpaca, holdings(DOGE=0.1, PEPE=0.1), stop=True)
    assert sorted(alpaca.sent) == [("sell", "BONK", 99.0), ("sell", "DOGE", 10.0)] and out["stopped"]


def test_spread_stale_quote_and_small_orders_are_held_back(run):
    wide = FakeAlpaca(spread=0.9)
    out = run(wide, holdings(DOGE=0.1))
    assert wide.sent == [] and "spread" in out["actions"][0]["detail"] and out["actions"][0]["receipt"]["action"] == "blocked"
    stale = FakeAlpaca(quote_age=900)
    assert "stale" in run(stale, holdings(DOGE=0.1))["actions"][0]["detail"] and stale.sent == []
    tiny = FakeAlpaca()
    assert "minimum" in run(tiny, holdings(DOGE=0.001))["actions"][0]["detail"] and tiny.sent == []


def test_crypto_buys_use_cash_not_margin(run):
    alpaca = FakeAlpaca(cash=200.0)  # 80,000 of margin buying power, but only 200 of cash
    run(alpaca, holdings(DOGE=0.5), allocation_pct=25)
    assert alpaca.sent == [("buy", "DOGE", "190.00")]  # 95% of the cash, not 3,125
    assert am.crypto_room({"cash": "100"}) == Decimal("95.00") and am.crypto_room({}) == 0


def test_a_retried_check_cannot_buy_twice_and_open_orders_wait(run):
    alpaca = FakeAlpaca()
    run(alpaca, holdings(DOGE=0.1))
    alpaca.held.clear()  # as if the fill had not shown up in positions yet
    again = run(alpaca, holdings(DOGE=0.1))
    assert len(alpaca.sent) == 1 and again["actions"][0]["order"]["status"] == "already sent"
    busy = FakeAlpaca(open_orders=["DOGE"])
    assert run(busy, holdings(DOGE=0.1))["coins"][0]["action"] == "wait" and busy.sent == []


def test_a_refusal_is_reported_and_the_typed_confirmation_is_required(run):
    out = run(FakeAlpaca(refuse="Alpaca refused this order: insufficient balance"), holdings(DOGE=0.1))
    assert out["actions"][0]["status"] == "refused"
    with pytest.raises(Exception):
        am.AlpacaMemecoinCheck(key_id=KEYS["key_id"], secret_key=KEYS["secret_key"], confirm="yes")
    with pytest.raises(Exception):
        am.AlpacaMemecoinCheck(**KEYS, allocation_pct=50)


def test_only_the_paper_host_is_used_and_the_route_is_rate_limited():
    from loki.api.rate_limit import rule_for
    assert am._paper_exchange.__module__ == "loki.api.broker_alpaca"
    assert rule_for("POST", "/api/broker/alpaca/memecoin")

"""Auto day trading on an Alpaca paper account: order logic and every guard, against a fake Alpaca."""

import pytest
from fastapi import HTTPException

from loki.api import alpaca_daytrade as adt
from loki.api.rate_limit import rule_for

KEYS = {"key_id": "PKTESTKEY123456", "secret_key": "s" * 40, "confirm": "day trade paper money"}


def market(symbol, position=0, pending=None, status="open", price=100.0, **extra):
    return {"symbol": symbol, "status": status, "position": position, "pending": pending, "last": price,
            "now_price": price, "signal_at": "2026-11-12T15:00:00+00:00", **extra}


class FakeAlpaca:
    """Just enough of Alpaca's paper API: account, clock, positions, orders, assets."""

    def __init__(self, equity=100_000.0, last_equity=100_000.0, is_open=True, held=None, open_orders=(), easy=True):
        self.equity, self.last_equity, self.is_open = equity, last_equity, is_open
        self.held = dict(held or {})
        self.open_orders, self.easy, self.sent = list(open_orders), easy, []

    def __call__(self, method, path, keys, body=None):
        assert keys.key_id == KEYS["key_id"]
        if path == "/v2/account":
            return {"equity": str(self.equity), "last_equity": str(self.last_equity), "status": "ACTIVE"}
        if path == "/v2/clock":
            return {"is_open": self.is_open, "next_open": "2026-11-13T14:30:00Z", "next_close": "2026-11-12T21:00:00Z"}
        if path == "/v2/positions":
            return [{"symbol": s, "qty": str(abs(q)), "side": "short" if q < 0 else "long"} for s, q in self.held.items() if q]
        if path.startswith("/v2/orders?status=open"):
            return [{"symbol": s} for s in self.open_orders]
        if path.startswith("/v2/assets/"):
            return {"shortable": self.easy, "easy_to_borrow": self.easy}
        if method == "DELETE" and path.startswith("/v2/positions/"):
            symbol = path.rsplit("/", 1)[1]
            self.sent.append(("close", symbol, self.held.pop(symbol, 0)))
            return {"id": "00000000-0000-0000-0000-000000000001", "symbol": symbol, "status": "accepted"}
        if method == "POST" and path == "/v2/orders":
            shares = int(body["qty"]) * (1 if body["side"] == "buy" else -1)
            assert body["type"] == "market" and body["time_in_force"] == "day" and not body["extended_hours"]
            self.sent.append(("order", body["symbol"], shares))
            self.held[body["symbol"]] = self.held.get(body["symbol"], 0) + shares
            return {"id": "00000000-0000-0000-0000-000000000002", "symbol": body["symbol"], "qty": body["qty"], "side": body["side"]}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(adt, "FLAT_WAIT_SECONDS", (0.0,))

    def go(alpaca, markets, **flags):
        plan = {"markets": markets, "weight_per_market": round(1 / len(markets), 5), "served_at": "2026-11-12T15:00:20+00:00",
                "generated_at": "2026-11-12T15:00:20+00:00", "record": {}}
        monkeypatch.setattr(adt, "_paper_exchange", alpaca)
        from loki.api import ai_paper
        monkeypatch.setattr(ai_paper, "ai_daytrade", lambda response, mode="stocks": plan)
        return adt._check(adt.AlpacaDayTradeCheck(**KEYS, **flags))
    return go


def test_sizes_and_sides_follow_the_rules():
    assert adt.wanted_side(market("QQQ", position=1)) == 1
    assert adt.wanted_side(market("QQQ", position=1, pending=0)) == 0  # a decision on the latest bar is acted on now
    assert adt.wanted_side(market("QQQ", pending=-1)) == -1
    assert adt.wanted_side(market("QQQ", position=1, skipped=True)) == 0
    assert adt.wanted_side(market("QQQ", position=1, status="closed")) == 0
    assert adt.wanted_side(market("QQQ", position=1, waiting=True)) == 0
    assert adt.target_shares(1, 120_000, 1 / 6, 100.0) == 190  # US$20,000 x 0.95 / 100
    assert adt.target_shares(-1, 120_000, 1 / 6, 100.0) == -190
    assert adt.target_shares(1, 10_000_000, 1 / 6, 100.0) == 475  # capped at US$50,000 a market
    assert adt.target_shares(1, 10_000_000, 1 / 6, 1.0) == adt.MAX_SHARES
    assert adt.signed_shares({"qty": "-12", "side": "short"}) == -12 and adt.signed_shares({"qty": "12", "side": "long"}) == 12


def test_opens_reverses_and_closes_only_when_the_direction_changes(run):
    alpaca = FakeAlpaca(held={"NVDA": 190, "TSLA": 190, "AMD": -190})
    out = run(alpaca, [market("QQQ", pending=1), market("NVDA", position=1), market("TSLA", pending=-1),
                       market("AMD", pending=0), market("AMZN"), market("ARKK", position=1)], start=False)
    by = {r["symbol"]: r["action"] for r in out["markets"]}
    assert by == {"QQQ": "open", "NVDA": "hold", "TSLA": "close_then_open", "AMD": "close", "AMZN": "none", "ARKK": "open"}
    assert alpaca.held == {"QQQ": 158, "NVDA": 190, "TSLA": -158, "ARKK": 158}  # 100,000 / 6 x 0.95 / 100 = 158
    assert ("close", "TSLA", 190) in alpaca.sent and alpaca.sent.index(("close", "TSLA", 190)) < alpaca.sent.index(("order", "TSLA", -158))
    assert all(a["status"] == "sent" for a in out["actions"]) and len(out["actions"]) == 5


def test_never_touches_other_holdings_and_refuses_to_start_on_its_own_markets(run):
    alpaca = FakeAlpaca(held={"SPY": 50})
    run(alpaca, [market("QQQ", pending=1)], start=True)
    assert alpaca.held["SPY"] == 50 and ("close", "SPY", 50) not in alpaca.sent
    with pytest.raises(HTTPException) as caught:
        run(FakeAlpaca(held={"QQQ": 10}), [market("QQQ")], start=True)
    assert caught.value.status_code == 409 and "already holds QQQ" in caught.value.detail


def test_guards_minimum_equity_closed_market_and_orders_in_flight(run):
    with pytest.raises(HTTPException) as caught:
        run(FakeAlpaca(equity=20_000, last_equity=20_000), [market("QQQ", pending=1)])
    assert caught.value.status_code == 422 and "25,000" in caught.value.detail
    closed = FakeAlpaca(is_open=False)
    assert run(closed, [market("QQQ", pending=1)])["actions"] == [] and closed.sent == []
    busy = FakeAlpaca(open_orders=["QQQ"])
    out = run(busy, [market("QQQ", pending=1)])
    assert out["markets"][0]["action"] == "wait" and busy.sent == []


def test_shorts_only_easy_to_borrow_stocks(run):
    alpaca = FakeAlpaca(easy=False)
    out = run(alpaca, [market("ARKK", pending=-1)])
    assert alpaca.sent == [] and "easy to borrow" in out["markets"][0]["note"]


def test_daily_loss_stop_and_the_stop_button_close_everything(run):
    alpaca = FakeAlpaca(equity=97_900, last_equity=100_000, held={"QQQ": 150, "NVDA": -80})
    out = run(alpaca, [market("QQQ", position=1), market("NVDA", position=-1), market("TSLA", pending=1)])
    assert out["stopped_for_loss"] and alpaca.held == {} and not any(s[0] == "order" for s in alpaca.sent)
    stop = FakeAlpaca(held={"QQQ": 150})
    out = run(stop, [market("QQQ", position=1)], stop=True)
    assert stop.held == {} and out["stopped"]


def test_endpoint_needs_the_typed_confirmation_and_is_rate_limited():
    with pytest.raises(Exception):
        adt.AlpacaDayTradeCheck(key_id=KEYS["key_id"], secret_key=KEYS["secret_key"], confirm="yes")
    assert rule_for("POST", "/api/broker/alpaca/daytrade")
    assert adt._paper_exchange.__module__ == "loki.api.broker_alpaca"  # the paper-only host is the only one used

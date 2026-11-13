"""Auto day trading on an Alpaca paper account: order logic and every guard, against a fake Alpaca."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from loki.api import alpaca_daytrade as adt
from loki.api.rate_limit import rule_for

NOW = datetime(2026, 11, 12, 15, 0, 20, tzinfo=timezone.utc)  # the plan's served_at in these tests
KEYS = {"key_id": "PKTESTKEY123456", "secret_key": "s" * 40, "confirm": "day trade paper money"}


def market(symbol, position=0, pending=None, status="open", price=100.0, **extra):
    return {"symbol": symbol, "status": status, "position": position, "pending": pending, "last": price,
            "now_price": price, "signal_at": "2026-11-12T15:00:00+00:00", **extra}


class FakeAlpaca:
    """Just enough of Alpaca's paper API: account, clock, positions, orders, assets."""

    def __init__(self, equity=100_000.0, last_equity=100_000.0, is_open=True, held=None, open_orders=(), easy=True,
                 bid=99.99, ask=100.01, account=None, buying_power=None, minutes_left=300, sip=True, quote_age=2,
                 fills=(), closed_orders=(), refuse=None):
        self.equity, self.last_equity, self.is_open = equity, last_equity, is_open
        self.held = dict(held or {})
        self.open_orders, self.easy, self.sent = list(open_orders), easy, []
        self.bid, self.ask, self.account, self.order_ids = bid, ask, account, set()
        self.buying_power = 2 * equity if buying_power is None else buying_power
        self.minutes_left, self.sip, self.quote_age, self.feeds = minutes_left, sip, quote_age, []
        self.fills, self.closed_orders, self.refuse = list(fills), list(closed_orders), refuse

    def data(self, method, path, keys, body=None, base=None):
        assert "data.alpaca.markets" in base and "/quotes/latest?feed=" in path
        feed = path.rsplit("=", 1)[1]
        self.feeds.append(feed)
        if feed == "sip" and not self.sip:
            raise HTTPException(422, "Alpaca refused this (403): subscription does not permit querying recent SIP data")
        stamp = (NOW - timedelta(seconds=self.quote_age)).isoformat().replace("+00:00", "Z")
        return {"symbol": path.split("/")[3], "quote": {"bp": self.bid, "ap": self.ask, "t": stamp}}

    def __call__(self, method, path, keys, body=None):
        assert keys.key_id == KEYS["key_id"]
        if path == "/v2/account" and self.account is not None:
            return self.account
        if path == "/v2/account":
            return {"equity": str(self.equity), "last_equity": str(self.last_equity), "buying_power": str(self.buying_power), "status": "ACTIVE"}
        if path == "/v2/clock":
            return {"is_open": self.is_open, "timestamp": NOW.isoformat(), "next_open": "2026-11-13T14:30:00Z",
                    "next_close": (NOW + timedelta(minutes=self.minutes_left)).isoformat()}
        if path.startswith("/v2/account/activities/FILL"):
            return self.fills
        if path.startswith("/v2/orders?status=closed"):
            return self.closed_orders
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
            if self.refuse:
                raise HTTPException(422, self.refuse)
            if body["client_order_id"] in self.order_ids:
                raise HTTPException(422, "Alpaca refused this order: client_order_id must be unique")
            self.order_ids.add(body["client_order_id"])
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
        monkeypatch.setattr(adt, "_exchange", alpaca.data)
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
        run(FakeAlpaca(equity=1_500, last_equity=1_500), [market("QQQ", pending=1)])
    assert caught.value.status_code == 422 and "2,000" in caught.value.detail and "margin" in caught.value.detail
    assert run(FakeAlpaca(equity=20_000, last_equity=20_000), [market("QQQ", pending=1)])["actions"][0]["status"] == "sent"
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


# --- The risk firewall: stale signals, spreads, malformed answers, duplicates, receipts --------------
def test_sizes_use_exact_decimals_and_bad_prices_never_size_an_order():
    assert adt.target_shares(1, 100_000.0, 1 / 6, 0.1 + 0.2) == 5_000  # capped, and no float drift
    assert adt.target_shares(1, 30_000.0, 0.5, 15.0) == 950  # 15,000 x 0.95 / 15 exactly, not 949
    for price in (0.0, -5.0, float("nan"), float("inf")):
        assert adt.target_shares(1, 100_000.0, 1 / 6, price) == 0
    assert adt.target_shares(1, float("nan"), 1 / 6, 100.0) == 0 and adt.target_shares(1, 0.0, 1 / 6, 100.0) == 0


def test_a_stale_signal_never_opens_but_a_close_still_goes_through(run):
    old = "2026-11-12T14:50:00+00:00"  # ten minutes before the check
    alpaca = FakeAlpaca(held={"TSLA": 100})
    out = run(alpaca, [market("QQQ", pending=1, signal_at=old), market("TSLA", pending=-1, signal_at=old)])
    by = {r["symbol"]: r for r in out["markets"]}
    assert by["QQQ"]["action"] == "none" and "missed this signal" in by["QQQ"]["note"]
    assert by["TSLA"]["action"] == "close"  # out of the old long, but no late short
    assert alpaca.held == {} and [s[0] for s in alpaca.sent] == ["close"]


def test_a_wide_spread_or_missing_quote_holds_the_entry_back(run):
    wide = FakeAlpaca(bid=99.5, ask=100.5)
    out = run(wide, [market("QQQ", pending=1)])
    assert wide.sent == [] and out["actions"][0]["status"] == "held back" and "spread" in out["actions"][0]["detail"]
    crossed = FakeAlpaca(bid=101.0, ask=100.0)
    assert run(crossed, [market("QQQ", pending=1)])["actions"][0]["detail"].startswith("no clean quote")


def test_a_malformed_alpaca_answer_trades_nothing(run):
    for broken in ({"equity": "NaN", "last_equity": "100000"}, {"equity": None}, {"last_equity": "1"}, "maintenance"):
        alpaca = FakeAlpaca(account=broken)
        with pytest.raises(HTTPException) as caught:
            run(alpaca, [market("QQQ", pending=1)])
        assert caught.value.status_code == 502 and "malformed" in caught.value.detail and alpaca.sent == []


def test_a_retried_check_cannot_place_the_same_order_twice(run):
    alpaca = FakeAlpaca()
    first = run(alpaca, [market("QQQ", pending=1)])
    alpaca.held.clear()  # as if the fill had not shown up yet when the request was retried
    again = run(alpaca, [market("QQQ", pending=1)])
    orders = [s for s in alpaca.sent if s[0] == "order"]
    assert len(orders) == 1 and again["actions"][0]["order"]["status"] == "already sent"
    assert first["actions"][0]["order"]["client_order_id"] == again["actions"][0]["order"]["client_order_id"]


def test_every_order_carries_a_receipt_bound_to_the_policy(run):
    out = run(FakeAlpaca(held={"TSLA": 10}), [market("QQQ", pending=1), market("TSLA", pending=0)])
    receipts = [a["receipt"] for a in out["actions"]]
    assert len(receipts) == 2 and all(r["policy"] == adt.POLICY_HASH == out["policy_hash"] for r in receipts)
    opened = next(r for r in receipts if r["action"] == "open")
    assert opened["snapshot"]["spread_pct"] == pytest.approx(0.02, abs=1e-3) and "spread <= 0.1% (SIP)" in opened["checks_passed"]
    body = {k: v for k, v in opened.items() if k != "hash"}
    import hashlib, json
    assert opened["hash"] == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert all(r["trace_id"] == out["trace_id"] for r in receipts)


# --- Review points: refusals, buying power, the bell, quote feeds, blocked receipts ----------------
def test_a_broker_refusal_is_reported_and_never_stops_the_check(run):
    alpaca = FakeAlpaca(refuse="Alpaca refused this (403): insufficient buying power")
    out = run(alpaca, [market("QQQ", pending=1), market("NVDA", pending=1)])
    assert [a["status"] for a in out["actions"]] == ["refused", "refused"] and "buying power" in out["actions"][0]["detail"]


def test_new_positions_fit_inside_the_buying_power_alpaca_reports(run):
    alpaca = FakeAlpaca(buying_power=20_000)  # room: US$19,000 at 95%; each market wants 316 shares (US$31,600)
    out = run(alpaca, [market("QQQ", pending=1), market("NVDA", pending=1), market("TSLA", pending=1)])
    sent = {a["symbol"]: a["shares"] for a in out["actions"] if a["status"] == "sent"}
    assert sent == {"QQQ": 190}  # cut to the room left; nothing remains for the other two
    assert [a["status"] for a in out["actions"][1:]] == ["held back", "held back"] and "buying power" in out["actions"][2]["detail"]


def test_everything_closes_ten_minutes_before_alpacas_close(run):
    alpaca = FakeAlpaca(minutes_left=9, held={"QQQ": 100, "NVDA": -50})
    out = run(alpaca, [market("QQQ", position=1), market("NVDA", position=-1), market("TSLA", pending=1)])
    assert out["closing_time"] and alpaca.held == {} and not any(s[0] == "order" for s in alpaca.sent)
    calm = FakeAlpaca(minutes_left=11)
    assert not run(calm, [market("QQQ", pending=1)])["closing_time"] and calm.held == {"QQQ": 475}  # one market: the US$50,000 cap


def test_quotes_prefer_sip_and_record_the_feed_used(run):
    paid = FakeAlpaca()
    out = run(paid, [market("QQQ", pending=1)])
    assert paid.feeds == ["sip"] and out["actions"][0]["receipt"]["snapshot"]["feed"] == "SIP"
    free = FakeAlpaca(sip=False)
    out = run(free, [market("QQQ", pending=1)])
    assert free.feeds == ["sip", "iex"] and out["actions"][0]["receipt"]["snapshot"]["feed"] == "IEX"
    stale = FakeAlpaca(quote_age=600)
    out = run(stale, [market("QQQ", pending=1)])
    assert stale.sent == [] and "no clean quote" in out["actions"][0]["detail"]


def test_a_blocked_order_gets_a_receipt_saying_why(run):
    out = run(FakeAlpaca(bid=99.5, ask=100.5), [market("QQQ", pending=1)])
    blocked = out["actions"][0]
    assert blocked["status"] == "held back" and blocked["receipt"]["action"] == "blocked"
    assert "spread" in blocked["receipt"]["snapshot"]["why"] and blocked["receipt"]["snapshot"]["feed"] == "SIP"
    assert blocked["receipt"]["snapshot"]["drift"]["tier"] == "green"


def test_order_ids_carry_the_decision_price_for_slippage():
    cid = adt.client_order_id("QQQ", "2026-11-12T15:00:00+00:00", 5, 690.12)
    assert cid.endswith("-buy-r69012") and len(cid) <= 128
    orders = [{"client_order_id": cid, "filled_avg_price": "690.81"},
              {"client_order_id": adt.client_order_id("TSLA", "x", -3, 400.0), "filled_avg_price": "399.60"},
              {"client_order_id": "someone-else", "filled_avg_price": "1"}]
    assert adt.slippage_from_orders(orders) == [pytest.approx(10.0, abs=0.01), pytest.approx(10.0, abs=0.01)]


def _fills(results):
    """Round trips from a list of P&L results: buy 10 at 100, sell 10 at 100 + pnl/10, one per day."""
    out = []
    for i, pnl in enumerate(results):
        day = f"2026-10-{1 + i:02d}T15:00:00Z"
        out += [{"symbol": "QQQ", "side": "buy", "qty": "10", "price": "100", "transaction_time": day},
                {"symbol": "QQQ", "side": "sell", "qty": "10", "price": str(100 + pnl / 10), "transaction_time": day}]
    return out


def test_drift_alarm_shrinks_new_positions_when_live_results_fall_short(run, monkeypatch):
    from loki.api import ai_paper
    monkeypatch.setattr(ai_paper, "_daytrade_record", lambda: {"market_baselines": {"QQQ": {"win_days_pct": 45.0, "payoff": 1.6}}})
    healthy = run(FakeAlpaca(fills=_fills([30, -20] * 12)), [market("QQQ", pending=1)])
    assert healthy["drift"]["tier"] == "green" and healthy["actions"][0]["shares"] == 475
    poor = run(FakeAlpaca(fills=_fills([30] * 4 + [-20] * 20)), [market("QQQ", pending=1)])  # 17% wins vs 45% tested
    assert poor["drift"]["tier"] == "red" and poor["drift"]["z"] < -2.5 and poor["actions"][0]["shares"] == 47  # 475 x 0.1
    assert poor["actions"][0]["receipt"]["snapshot"]["drift"]["multiplier"] == 0.1
    short = run(FakeAlpaca(fills=_fills([-20] * 5)), [market("QQQ", pending=1)])
    assert short["drift"]["tier"] == "green" and "warming up" in short["drift"]["reasons"][0]


def test_drift_maths_by_hand():
    from loki_v8 import drift
    fills = [{"symbol": "QQQ", "side": "buy", "qty": 10, "price": 100, "day": "d1"},
             {"symbol": "QQQ", "side": "sell", "qty": 10, "price": 101, "day": "d1"},
             {"symbol": "TSLA", "side": "sell_short", "qty": 5, "price": 200, "day": "d1"},
             {"symbol": "TSLA", "side": "buy", "qty": 5, "price": 204, "day": "d1"},
             {"symbol": "AMD", "side": "buy", "qty": 3, "price": 50, "day": "d1"},  # still open: left out
             {"symbol": "SPY", "side": "buy", "qty": 1, "price": 600, "day": "d1"},
             {"symbol": "SPY", "side": "sell", "qty": 1, "price": 601, "day": "d1"}]  # not a Day Trader market
    assert drift.round_trips(fills, ["QQQ", "TSLA", "AMD"]) == [{"day": "d1", "symbol": "QQQ", "pnl": 10.0},
                                                               {"day": "d1", "symbol": "TSLA", "pnl": -20.0}]
    trips = [{"day": str(i), "symbol": "QQQ", "pnl": 1.0 if i < 9 else -1.0} for i in range(20)]  # 45% wins
    out = drift.assess(trips, {"QQQ": {"win_days_pct": 45.0, "payoff": 1.0}}, [1.0] * 20, 1.0)
    assert out["z"] == 0.0 and out["tier"] == "green" and out["multiplier"] == 1.0
    out = drift.assess(trips, {"QQQ": {"win_days_pct": 45.0, "payoff": 1.0}}, [3.0] * 20, 1.0)
    assert out["tier"] == "amber" and "slippage" in out["reasons"][0]

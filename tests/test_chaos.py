"""Chaos and property-based tests: the bots must degrade safely when the world misbehaves.

Injected: rate limits (429), server errors (503), lost order answers, malformed JSON, flash crashes,
stuck states, failing RPC endpoints and tampered logs. Property tests (Hypothesis) throw arbitrary
inputs at the filters and arbitrary price paths at the exit engine.
"""
import io
import time
import urllib.error

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st

import memecoin_shadow as ms
from loki_v8 import launch_filters as lf

# --- property tests ---------------------------------------------------------------------------------
json_value = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=8),
                          lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4), max_leaves=20)
report_keys = ["mint", "mintAuthority", "freezeAuthority", "rugged", "markets", "topHolders", "knownAccounts", "risks",
               "graphInsidersDetected", "transferFee", "totalMarketLiquidity"]


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(st.sampled_from(report_keys), json_value), st.one_of(st.none(), st.floats()), st.none() | st.tuples(st.text(), st.integers()))
def test_safety_never_crashes_on_any_report(report, bundle, cabal):
    reasons = lf.safety(report, launch_bundle_pct=bundle if bundle == bundle else None, cabal=cabal)
    assert isinstance(reasons, list) and all(isinstance(r, str) for r in reasons)


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(st.sampled_from(["volume", "txns", "liquidity"]), json_value) | st.none(), st.none() | st.floats(min_value=0, max_value=1e9))
def test_velocity_never_crashes_on_any_pair(pair, liquidity):
    reasons, figures = lf.velocity(pair, liquidity)
    assert isinstance(reasons, list) and figures["liquidity_usd"] >= 0


@settings(max_examples=200, deadline=None)
@given(st.lists(json_value, max_size=6))
def test_launch_parsers_never_crash_on_junk_transactions(txs):
    assert isinstance(lf.launch_buyers(txs, "M", ["POOL"]), dict)
    assert lf.funder_of(txs[0] if txs else None, "W") is None or isinstance(lf.funder_of(txs[0], "W"), str)


@settings(max_examples=400, deadline=None)
@given(st.lists(st.floats(min_value=0.0, max_value=50.0) | st.just(float("nan")) | st.just(float("inf")), min_size=1, max_size=60))
def test_exit_engine_invariants_hold_on_any_price_path(path):
    pos, plan, sold = lf.Position("M", 1.0, opened_at=0), lf.ExitPlan(), 0.0
    for i, price in enumerate(path):
        before = pos.remaining
        sales = lf.step(pos, price, i * 30.0, plan)
        sold += sum(share for share, _ in sales)
        assert 0.0 <= pos.remaining <= 1.0 + 1e-9
        assert abs(sold + pos.remaining - 1.0) < 1e-9  # nothing is sold twice or lost
        if before <= 1e-9:
            assert sales == []  # a closed position never sells again
    assert len(pos.taken) == len(set(pos.taken))  # each take-profit level at most once


# --- the shadow runner under fire -------------------------------------------------------------------
GOOD_REPORT = {"mint": "MINT", "mintAuthority": None, "freezeAuthority": None, "risks": [], "graphInsidersDetected": 0,
               "markets": [{"pubkey": "CURVE", "marketType": "pump_fun", "lp": {}}], "knownAccounts": {"CURVE": {"type": "AMM"}},
               "topHolders": [{"owner": "CURVE", "pct": 80, "address": "CURVEACC"}] + [{"owner": f"W{i}", "pct": 1, "address": f"ACC{i}"} for i in range(10)],
               "totalMarketLiquidity": 10000, "token": {"decimals": 6}, "tokenMeta": {"symbol": "TEST"}}
GOOD_PAIR = [{"volume": {"m5": 30000}, "txns": {"m5": {"buys": 90, "sells": 30}}, "liquidity": {"usd": 10000}}]


class Market:
    """A fake Jupiter: ``price`` USD per token, SOL at $100; quotes follow the current price."""

    def __init__(self, price=0.001, sell_haircut=0.02):
        self.price, self.sell_haircut, self.dead = price, sell_haircut, False

    def quote(self, inp, out, amount):
        if self.dead:
            return None
        if inp == ms.SOL and out == ms.USDC:
            return {"outAmount": str(100 * 10**6)}
        if inp == ms.SOL:  # buy: lamports -> token units (6 decimals)
            usd = amount / 1e9 * 100
            return {"outAmount": str(int(usd / self.price * 10**6))}
        usd = amount / 10**6 * self.price * (1 - self.sell_haircut)
        return {"outAmount": str(int(usd / 100 * 1e9))}


@pytest.fixture
def shadow(monkeypatch, tmp_path):
    market = Market()
    feeds = {"report": GOOD_REPORT, "pairs": GOOD_PAIR}
    monkeypatch.setattr(ms, "quote", market.quote)
    monkeypatch.setattr(ms, "http", lambda url, body=None, tries=3: feeds["report"] if "/report" in url else feeds["pairs"])
    monkeypatch.setattr(ms, "launch_check", lambda mint, pools: (5.0, None))
    monkeypatch.setattr(ms, "holder_balances", lambda accounts: {a: 100.0 for a in accounts})
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)
    ms._calls.clear()
    con = ms.db(tmp_path / "shadow.db")
    return con, market, feeds


def state(con):
    return con.execute("select state, pnl_usd from trades order by id desc limit 1").fetchone()


def test_entry_then_a_90_percent_flash_crash_exits_through_the_hard_stop(shadow):
    con, market, _ = shadow
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)
    assert state(con)[0] == "OPEN"
    market.price *= 0.10  # flash crash
    ms.manage(con, 100.0, lf.ExitPlan(), 0.0003)
    st_, pnl = state(con)
    assert st_ == "CLOSED" and -20.0 < pnl < -17.0  # ~90% of US$20 lost, plus fees: bounded, recorded, no crash
    assert ms.verify(con)["ok"] and ms.report(con)["trades_by_state"] == {"CLOSED": 1}


def test_price_running_away_before_landing_fails_safe_for_the_fee_only(shadow, monkeypatch):
    con, market, _ = shadow
    original = market.quote
    calls = {"n": 0}

    def jumpy(inp, out, amount):
        if inp == ms.SOL and out == "MINT":
            calls["n"] += 1
            if calls["n"] == 2:  # the landing quote: the price ran 10% higher
                market.price *= 1.10
        return original(inp, out, amount)
    monkeypatch.setattr(ms, "quote", jumpy)
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)
    st_, pnl = state(con)
    assert st_ == "FAILED_SAFE" and pnl == pytest.approx(-0.03)  # 0.0003 SOL at $100


def test_a_honeypot_round_trip_is_rejected_before_any_trade(shadow):
    con, market, _ = shadow
    market.sell_haircut = 0.40  # selling right after buying loses 40%: a hidden tax
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)
    assert con.execute("select count(*) from trades").fetchone()[0] == 0
    assert "honeypot" in con.execute("select reasons from tokens").fetchone()[0]


def test_no_route_three_times_counts_the_rest_as_lost(shadow):
    con, market, _ = shadow
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)
    market.dead = True
    for _ in range(3):
        ms.manage(con, 100.0, lf.ExitPlan(), 0.0003)
    assert state(con) == ("CLOSED", pytest.approx(-20.03))


def test_a_stuck_pending_entry_is_rolled_back(shadow):
    con, _, _ = shadow
    con.execute("insert into trades (mint, opened_at, entry_usd, state, exits, proceeds_usd, costs_usd, peak) values ('X', ?, 20, 'PENDING_ENTRY', '[]', 0, 0, 1)",
                (time.time() - 120,))
    ms.manage(con, 100.0, lf.ExitPlan(), 0.0003)
    assert state(con)[0] == "FAILED_SAFE"


def test_kill_switch_and_error_breaker_stop_new_entries(shadow):
    con, _, _ = shadow
    con.execute("insert into trades (mint, state, pnl_usd, closed_at, exits) values ('OLD', 'CLOSED', -150, ?, '[]')", (time.time(),))
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003, kill_usd=100.0)
    assert "kill switch" in con.execute("select reasons from tokens where mint='MINT'").fetchone()[0]
    con.execute("delete from trades"); con.execute("delete from tokens"); con.commit()
    ms._calls.extend([False] * 30)
    assert ms.breaker_open()
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)
    assert "failing" in con.execute("select reasons from tokens where mint='MINT'").fetchone()[0]


@pytest.mark.parametrize("junk", ["garbage", 42, [], [1, 2], {"mint": None}, {"mint": "MINT", "topHolders": "x", "markets": 7, "risks": [None]}])
def test_malformed_answers_never_crash_a_pass(shadow, junk):
    con, _, feeds = shadow
    feeds["report"], feeds["pairs"] = junk, junk
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)  # must not raise
    assert con.execute("select count(*) from trades").fetchone()[0] == 0


def test_illegal_state_changes_raise_and_a_tampered_ledger_is_caught(shadow):
    con, _, _ = shadow
    ms.screen(con, "MINT", 100.0, 20.0, 0.0003)
    tid = con.execute("select id from trades").fetchone()[0]
    ms.set_state(con, tid, "CLOSED", pnl_usd=0.0)
    with pytest.raises(ValueError):
        ms.set_state(con, tid, "OPEN")  # a closed trade can never reopen
    with pytest.raises(ValueError):
        lf.advance("PENDING_ENTRY", "PARTIAL_EXIT")
    assert ms.verify(con)["ok"]
    con.execute("update ledger set body='{\"usd\": 1}' where seq=1"); con.commit()
    assert ms.verify(con) == {"ok": False, "entries": 2, "broken_at": 1}


def test_rpc_fails_over_and_rests_a_failing_endpoint(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URLS", "https://first.example,https://second.example")
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)
    ms._down_until.clear()
    hits = []

    def fake_http(url, body=None, tries=3):
        hits.append(url)
        return None if "first" in url else {"result": 7}
    monkeypatch.setattr(ms, "http", fake_http)
    assert ms.rpc("getHealth", []) == 7 and hits == ["https://first.example", "https://second.example"]
    hits.clear()
    assert ms.rpc("getHealth", []) == 7 and hits == ["https://second.example"]  # the failing one sits out


def test_http_retries_rate_limits_and_records_failures(monkeypatch):
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)
    answers = [urllib.error.HTTPError("u", 429, "slow down", {}, None), urllib.error.HTTPError("u", 503, "down", {}, None)]

    class Ok(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, timeout=20):
        if answers:
            raise answers.pop(0)
        return Ok(b'{"ok": true}')
    monkeypatch.setattr(ms.urllib.request, "urlopen", opener)
    ms._calls.clear()
    assert ms.http("https://x.example") == {"ok": True}  # a 429 then a 503, then it answered
    monkeypatch.setattr(ms.urllib.request, "urlopen", lambda r, timeout=20: (_ for _ in ()).throw(urllib.error.HTTPError("u", 503, "down", {}, None)))
    assert ms.http("https://x.example") is None and list(ms._calls)[-1] is False
    monkeypatch.setattr(ms.urllib.request, "urlopen", lambda r, timeout=20: Ok(b"<html>not json"))
    assert ms.http("https://x.example") is None


# --- orders whose answer is lost ------------------------------------------------------------------
from loki.api.broker_alpaca import AlpacaPaperKeys  # noqa: E402
from loki.api.order_safety import submit_once  # noqa: E402

KEYS = AlpacaPaperKeys(key_id="PKTESTKEY123456", secret_key="s" * 40)
ORDER = {"symbol": "DOGE/USD", "notional": "10.00", "side": "buy", "type": "market", "time_in_force": "ioc", "client_order_id": "loki-mc-DOGE-1-buy"}


def exchange(post_error=None, lookup=None, lookup_error=None):
    calls = []

    def call(method, path, keys, body=None):
        calls.append((method, path))
        if method == "POST":
            if post_error:
                raise post_error
            return {"id": "A1", "status": "accepted", "symbol": "DOGE/USD"}
        if lookup_error:
            raise lookup_error
        return lookup
    return call, calls


def test_a_lost_answer_is_looked_up_before_anything_is_reported():
    call, calls = exchange(post_error=HTTPException(503, "Alpaca is unreachable"), lookup={"id": "A1", "status": "filled"})
    out = submit_once(call, KEYS, ORDER)
    assert out["status"] == "filled" and "landed" in out["verified"] and calls[1] == ("GET", "/v2/orders:by_client_order_id?client_order_id=loki-mc-DOGE-1-buy")


def test_a_lost_answer_with_no_order_on_record_says_nothing_was_placed():
    call, _ = exchange(post_error=HTTPException(504, "timeout"), lookup_error=HTTPException(502, "Alpaca's paper endpoint answered 404; its API may have moved."))
    with pytest.raises(HTTPException) as caught:
        submit_once(call, KEYS, ORDER)
    assert caught.value.status_code == 503 and "nothing was placed" in caught.value.detail
    call, _ = exchange(post_error=HTTPException(503, "down"), lookup_error=HTTPException(503, "still down"))
    with pytest.raises(HTTPException) as caught:
        submit_once(call, KEYS, ORDER)
    assert "could not be looked up" in caught.value.detail and "cannot be placed twice" in caught.value.detail


def test_duplicates_refusals_and_bad_keys_are_not_retried_blindly():
    call, calls = exchange(post_error=HTTPException(422, "Alpaca refused this order: client_order_id must be unique"))
    assert submit_once(call, KEYS, ORDER)["status"] == "already sent" and len(calls) == 1
    for error in (HTTPException(422, "insufficient balance"), HTTPException(401, "rejected those keys")):
        call, calls = exchange(post_error=error)
        with pytest.raises(HTTPException):
            submit_once(call, KEYS, ORDER)
        assert len(calls) == 1  # no lookup: Alpaca answered clearly

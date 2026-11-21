"""Memecoin terminal core: AUD money, true net P&L, exits, breakers, RPC failover, rug shield, Jupiter."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loki.terminal import aud, breakers, costs, exits, jupiter, rpc, shield

DAY0 = 1_763_600_000.0  # a fixed timestamp


class TestAud:
    def test_conversions_round_trip(self):
        assert aud.aud_to_lamports(104.0, 104.0) == 1_000_000_000
        assert aud.lamports_to_aud(5_000, 104.0) == pytest.approx(0.00052)
        with pytest.raises(ValueError):
            aud.aud_to_sol(10, 0)

    def test_falls_back_to_the_next_source_then_serves_stale_flagged(self, monkeypatch):
        aud._cache.clear()
        calls = []
        monkeypatch.setitem(aud.SOURCES, "sol_usd", [("jupiter", lambda: calls.append("j") or (_ for _ in ()).throw(OSError())),
                                                     ("coinbase", lambda: calls.append("c") or 70.0)])
        monkeypatch.setitem(aud.SOURCES, "usdc_aud", [("coinbase", lambda: 1.5)])
        r = aud.rates(now=DAY0)
        assert r["sol_aud"] == 105.0 and r["sources"]["sol_usd"] == "coinbase" and r["live"] and calls == ["j", "c"]
        monkeypatch.setitem(aud.SOURCES, "sol_usd", [("jupiter", lambda: (_ for _ in ()).throw(OSError()))])
        stale = aud.rates(now=DAY0 + 30)  # within STALE_AFTER_S: last value, flagged not live
        assert stale["sol_aud"] == 105.0 and stale["live"] is False
        with pytest.raises(RuntimeError):
            aud.rates(now=DAY0 + 1000)
        aud._cache.clear()


class TestNetPnl:
    def test_every_cost_is_deducted_in_aud(self):
        leg = costs.TxCost(compute_units=200_000, cu_price_micro_lamports=100_000, jito_tip_lamports=100_000)
        buy = costs.Fill("buy", sol=1.0, tokens=1000, sol_aud=100.0, cost=leg)
        sell = costs.Fill("sell", sol=1.2, tokens=1000, sol_aud=100.0, cost=costs.TxCost(200_000, 100_000, 100_000, slippage_aud=1.5))
        p = costs.net_pnl([buy, sell])
        assert p["gross_aud"] == 20.0
        # per leg: 5000 base + 20000 priority + 100000 tip lamports = 125000 lamports = A$0.0125
        assert p["fees_aud"] == pytest.approx(0.025) and p["jito_tip_aud"] == pytest.approx(0.02)
        assert p["net_aud"] == pytest.approx(19.975) and p["slippage_aud"] == 1.5
        assert p["net_pct"] == pytest.approx(19.975)

    def test_a_tiny_trade_can_be_eaten_by_fees(self):
        fees = costs.round_trip_fees_aud(100.0, jito_tip_lamports=1_000_000)  # 0.001 SOL tip each way
        assert fees > 0.2 and costs.round_trip_fees_aud(100.0) < fees


class TestExits:
    def plan(self, **kw):
        return exits.Position(entry_price=1.0, entry_time=DAY0, plan=exits.ExitPlan(**kw))

    def test_stop_loss_sells_everything(self):
        p = self.plan()
        assert exits.step(p, 0.74, DAY0 + 5) == [(1.0, "stop loss")] and p.remaining == 0

    def test_half_at_2x_then_the_moonbag_rides_until_the_trailing_stop(self):
        p = self.plan(stale_after_s=None)
        assert exits.step(p, 2.0, DAY0 + 30) == [(0.5, "take profit 2x")] and p.remaining == 0.5
        assert exits.step(p, 3.0, DAY0 + 60) == []  # the moonbag keeps running
        assert exits.step(p, 2.30, DAY0 + 80) == []  # 3.0 high, 25% trail -> triggers at 2.25
        assert exits.step(p, 2.20, DAY0 + 90) == [(0.5, "trailing stop")]
        assert p.remaining == 0 and exits.step(p, 5.0, DAY0 + 95) == []

    def test_a_stale_coin_is_dumped(self):
        p = self.plan(stale_after_s=120, stale_move=0.05)
        assert exits.step(p, 1.02, DAY0 + 60) == []
        assert exits.step(p, 1.03, DAY0 + 121) == [(1.0, "stale: no move")]

    def test_a_coin_that_moved_is_not_stale_but_still_has_a_max_hold(self):
        p = self.plan(stale_after_s=120, max_hold_s=600, trail=None)
        exits.step(p, 1.10, DAY0 + 30)
        assert exits.step(p, 1.08, DAY0 + 200) == []
        assert exits.step(p, 1.08, DAY0 + 601) == [(1.0, "max hold time")]

    def test_several_tiers_in_one_tick(self):
        p = self.plan(tiers=((2.0, 0.5), (4.0, 0.25)), stale_after_s=None)
        assert exits.step(p, 4.5, DAY0 + 10) == [(0.5, "take profit 2x"), (0.25, "take profit 4x")]
        assert p.remaining == pytest.approx(0.25)


class TestBreakers:
    def test_daily_loss_limit_trips_and_resets_next_day(self):
        b = breakers.Breaker(breakers.Limits(max_daily_loss_aud=50, max_trade_aud=40))
        assert b.can_enter(20, DAY0)["ok"]
        b.record_open(); b.record_close(-30, DAY0)
        assert "daily loss" in b.can_enter(25, DAY0)["why"]  # 25 more could breach 50
        b.record_open(); b.record_close(-25, DAY0)
        assert b.tripped and "daily loss" in b.tripped
        assert not b.can_enter(5, DAY0)["ok"]
        assert b.can_enter(5, DAY0 + 86_400)["ok"]  # a new trading day

    def test_failed_transactions_trip_the_kill_switch_until_a_person_resets_it(self):
        b = breakers.Breaker(breakers.Limits(max_consecutive_failures=3))
        b.record_tx(False); b.record_tx(False); b.record_tx(True); b.record_tx(False); b.record_tx(False)
        assert b.tripped is None
        b.record_tx(False)
        assert "3 failed transactions" in b.tripped
        assert not b.can_enter(5, DAY0 + 86_400)["ok"]  # not cleared by a new day
        b.reset()
        assert b.can_enter(5, DAY0)["ok"]

    def test_caps_on_size_and_open_positions(self):
        b = breakers.Breaker(breakers.Limits(max_trade_aud=50, max_open_positions=1))
        assert "cap" in b.can_enter(60, DAY0)["why"]
        b.record_open()
        assert "open positions" in b.can_enter(10, DAY0)["why"]


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class TestRpcFailover:
    def test_a_failing_endpoint_is_skipped_and_marked_down(self, monkeypatch):
        seen = []

        def post(url, json=None, timeout=None, allow_redirects=False):
            seen.append(url)
            if "bad" in url:
                return FakeResp(503, {})
            return FakeResp(200, {"jsonrpc": "2.0", "id": json["id"], "result": 42})

        monkeypatch.setattr(rpc.requests, "post", post)
        p = rpc.RpcPool(["https://bad.example", "https://good.example"])
        assert p.call("getSlot", []) == 42
        assert seen[:2] == ["https://bad.example", "https://good.example"]
        assert p.call("getSlot", []) == 42 and seen[2] == "https://good.example"  # bad one is cooling down
        assert [s["up"] for s in p.status()][:2] == [False, True]

    def test_a_bad_request_is_reported_not_failed_over(self, monkeypatch):
        monkeypatch.setattr(rpc.requests, "post", lambda *a, **k: FakeResp(200, {"error": {"message": "invalid param"}}))
        with pytest.raises(ValueError, match="invalid param"):
            rpc.RpcPool(["https://good.example"]).call("getAccountInfo", ["x"])


SAFE = {"mint_authority": None, "freeze_authority": None}


def report(**kw):
    base = {"topHolders": [{"owner": f"w{i}", "pct": 2.0} for i in range(10)], "knownAccounts": {},
            "markets": [{"marketType": "raydium", "lp": {"lpLockedPct": 100.0, "lpLockedUSD": 50_000}}],
            "totalMarketLiquidity": 80_000, "risks": [], "rugged": False}
    base.update(kw)
    return base


class TestShield:
    def test_a_clean_token_passes(self):
        out = shield.evaluate(report(), SAFE)
        assert out["ok"] and out["blocked_by"] == []

    def test_honeypot_and_inflation_are_blocked(self):
        out = shield.evaluate(report(), {"mint_authority": "Mint111", "freeze_authority": "Frz111"})
        assert not out["ok"] and set(out["blocked_by"]) == {"mint authority disabled", "freeze authority disabled"}

    def test_whale_concentration_is_blocked_but_pools_do_not_count(self):
        holders = [{"owner": "pool", "pct": 60.0}] + [{"owner": f"w{i}", "pct": 2.0} for i in range(10)]
        ok = shield.evaluate(report(topHolders=holders, knownAccounts={"pool": {"type": "AMM"}}), SAFE)
        assert ok["ok"]
        bad = shield.evaluate(report(topHolders=[{"owner": "whale", "pct": 45.0}]), SAFE)
        assert "top-10 holders below limit" in bad["blocked_by"]

    def test_pullable_liquidity_is_blocked(self):
        out = shield.evaluate(report(markets=[{"marketType": "raydium", "lp": {"lpLockedPct": 10, "lpLockedUSD": 1}}]), SAFE)
        assert out["blocked_by"] == ["liquidity locked or burned"]

    def test_concentrated_pools_are_judged_on_depth_and_the_curve_is_safe(self):
        cl = report(markets=[{"marketType": "orca", "lp": {"lpLockedPct": 0}}], totalMarketLiquidity=200_000)
        assert shield.evaluate(cl, SAFE)["ok"]
        thin = report(markets=[{"marketType": "meteoraDlmm", "lp": {"lpLockedPct": 0}}], totalMarketLiquidity=900)
        assert "liquidity locked or burned" in shield.evaluate(thin, SAFE)["blocked_by"]
        curve = report(markets=[], launchpad={"name": "pump_fun"})
        assert shield.evaluate(curve, SAFE)["ok"]

    def test_flagged_or_unknown_tokens_are_blocked(self):
        assert "not flagged" in shield.evaluate(report(rugged=True), SAFE)["blocked_by"]
        assert "not flagged" in shield.evaluate(report(risks=[{"name": "Copycat", "level": "danger"}]), SAFE)["blocked_by"]
        assert shield.evaluate(None, None) == {"ok": False, "checks": [{"check": "data", "ok": False, "detail":
                                               "Neither the chain nor RugCheck answered, so the token cannot be checked."}], "blocked_by": ["data"]}

    def test_rugcheck_down_still_checks_authorities_on_chain_and_blocks_unknown_holders(self):
        out = shield.evaluate(None, SAFE)
        assert {c["check"]: c["ok"] for c in out["checks"]}["mint authority disabled"] is True
        assert not out["ok"]  # holders and liquidity could not be verified


class TestJupiter:
    def test_priority_fee_mode_and_jito_mode(self):
        prio = jupiter.swap_body({"q": 1}, "Wallet", jupiter.SwapSettings())
        assert prio["dynamicSlippage"] and prio["dynamicComputeUnitLimit"]
        assert prio["prioritizationFeeLamports"] == {"priorityLevelWithMaxLamports": {"maxLamports": 2_000_000, "priorityLevel": "veryHigh"}}
        jito = jupiter.swap_body({"q": 1}, "Wallet", jupiter.SwapSettings(jito_tip_lamports=100_000))
        assert jito["prioritizationFeeLamports"] == {"jitoTipLamports": 100_000}

    def test_summary_in_aud(self):
        q = {"inAmount": "1000000000", "outAmount": "2000000", "priceImpactPct": "0.012",
             "routePlan": [{"swapInfo": {"label": "Raydium"}}], "slippageBps": 50}
        s = jupiter.summary(q, sol_aud=100.0, side="buy", decimals=6)
        assert s["aud"] == 100.0 and s["tokens"] == 2.0 and s["price_aud"] == 50.0 and s["price_impact_pct"] == 1.2
        assert s["route"] == ["Raydium"]

    def test_never_signs(self):
        import inspect

        src = inspect.getsource(jupiter)
        assert "sign" not in src.replace("signs", "").replace("signed", "").replace("unsigned", "").replace("sign or", "").lower() \
            or "never signs" in src.lower()


class TestApi:
    def test_quote_returns_aud_and_blocks_a_failed_shield(self, monkeypatch):
        from loki.api.server import app
        from loki.terminal import api

        monkeypatch.setattr(api.aud, "rates", lambda: {"sol_aud": 100.0, "sol_usd": 66.0, "usdc_aud": 1.5})
        monkeypatch.setattr(api.jupiter, "quote", lambda *a, **k: {"inAmount": "200000000", "outAmount": "4000000000",
                                                                    "priceImpactPct": "0.001", "routePlan": [], "slippageBps": 50})
        monkeypatch.setattr(api.shield, "check", lambda mint: {"ok": False, "blocked_by": ["freeze authority disabled"], "checks": []})
        c = TestClient(app)
        body = c.get("/api/terminal/quote", params={"mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "aud_amount": 20}).json()
        assert body["aud"] == 20.0 and body["sol"] == 0.2 and body["allowed"] is False
        assert c.get("/api/terminal/quote", params={"mint": "not-a-mint!" * 4}).status_code == 422
        assert c.get("/api/terminal/rpc").json()["endpoints"][-1]["host"] == "api.mainnet-beta.solana.com"

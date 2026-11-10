"""Forward-test rebalancer: paper-only, band-limited, sells before buys, skips what Alpaca can't trade."""

import alpaca_forward as af

PLAN = {"positions": [
    {"symbol": "SPY", "weight": 0.30},
    {"symbol": "BTC-USD", "weight": 0.10},
    {"symbol": "BNB-USD", "weight": 0.05},
    {"symbol": "GLD", "weight": 0.0},
    {"symbol": "TLT", "weight": 0.101},
]}


def held(**values):
    return {sym: {"market_value": str(v[0]), "qty": str(v[1]), "current_price": str(v[2])} for sym, v in values.items()}


def test_paper_host_is_hard_wired():
    assert af.PAPER_URL == "https://paper-api.alpaca.markets"


def test_buys_to_target_and_skips_unsupported_and_small_moves():
    trades, notes = af.plan_trades(PLAN, 10_000, held(TLT=(1000, 10, 100)), stocks_open=True)
    by = {t["symbol"]: t for t in trades}
    assert by["SPY"]["side"] == "buy" and by["SPY"]["notional"] == 3000.0
    assert by["BTC/USD"]["crypto"] and by["BTC/USD"]["notional"] == 1000.0
    assert "TLT" not in by  # 1,010 target vs 1,000 held: inside the 2% band
    assert any("BNB-USD" in n and "cash" in n for n in notes)


def test_exits_and_trims_sell_before_buys():
    trades, _ = af.plan_trades(PLAN, 10_000, held(GLD=(800, 2, 400), SPY=(4000, 8, 500)), stocks_open=True)
    sides = [(t["symbol"], t["side"]) for t in trades]
    assert sides[0][1] in ("sell", "close") and ("GLD", "close") in sides
    spy = next(t for t in trades if t["symbol"] == "SPY")
    assert spy["side"] == "sell" and abs(spy["qty"] - 2.0) < 1e-9  # 4,000 -> 3,000 at $500 = 2 shares


def test_closed_stock_market_defers_stocks_but_trades_crypto():
    trades, notes = af.plan_trades(PLAN, 10_000, {}, stocks_open=False)
    assert [t["symbol"] for t in trades] == ["BTC/USD"]
    assert any(n.startswith("SPY") and "closed" in n for n in notes)


def test_performance_summary():
    perf = af.performance({"equity": [25000, 25500, 24990, 26000], "timestamp": [1, 2, 3, 4]})
    assert perf["return_pct"] == 4.0 and perf["max_drawdown_pct"] == -2.0 and perf["days"] == 4


def test_performance_includes_todays_equity():
    # Alpaca's daily history stops at yesterday; the live balance must count.
    perf = af.performance({"equity": [25000, 24998], "timestamp": [86400 * 20000, 86400 * 20001]}, 25058.68)
    assert perf["equity"] == 25058.68 and perf["return_pct"] == 0.23 and perf["since"] == "2024-10-04"
    assert af.performance({"equity": [25000, 25100], "timestamp": [1, 2]}, 25100.0)["days"] == 2


def test_idle_cash_goes_into_treasury_bills():
    plan = {"positions": [{"symbol": "SPY", "weight": 0.5}], "cash_parking": {"symbol": "SGOV", "weight": 0.5, "yield_pct": 3.9}}
    trades, _ = af.plan_trades(plan, 10_000, {}, stocks_open=True)
    assert {(t["symbol"], t["side"], t["notional"]) for t in trades} == {("SPY", "buy", 5000.0), ("SGOV", "buy", 5000.0)}
    # When markets re-enter, the bills are sold first to pay for them.
    held = {"SGOV": {"market_value": 9000, "qty": 90, "current_price": 100}}
    trades, _ = af.plan_trades({"positions": [{"symbol": "SPY", "weight": 0.9}],
                                "cash_parking": {"symbol": "SGOV", "weight": 0.1}}, 10_000, held, stocks_open=True)
    assert trades[0]["symbol"] == "SGOV" and trades[0]["side"] == "sell" and trades[0]["qty"] == 80

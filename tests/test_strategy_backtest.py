"""Tests for cost-aware, chronological paper strategy replay."""
import math
import time

import pytest

from loki.api.binance_market_data import MarketDataError, get_binance_spot_history
from loki.api.strategy_backtest import (
    BacktestInputError,
    _resolve_exit,
    run_strategy_backtest,
)


def candle_series(count=200):
    rows = []
    for index in range(count):
        close = 100.0
        row = {
            "time": 1_700_000_000.0 + index * 3600,
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": close,
            "volume": 100.0,
        }
        rows.append(row)
    rows[150].update(open=100.2, high=103.0, low=100.1, close=102.5, volume=300.0)
    rows[151].update(open=102.5, high=104.0, low=102.4, close=103.5, volume=100.0)
    return rows


def test_holdout_keeps_later_breakout_out_of_training_and_fills_next_bar():
    result = run_strategy_backtest(
        candle_series(),
        strategy_key="breakout",
        fee_bps=0,
        slippage_bps=0,
    )

    assert result["training"]["trade_count"] == 0
    assert result["out_of_sample"]["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == candle_series()[151]["time"]
    assert trade["exit_reason"] == "target"
    assert trade["net_pnl_quote"] > 0


def test_fees_and_slippage_reduce_a_fixed_signal_result():
    candles = candle_series()
    free = run_strategy_backtest(candles, "breakout", fee_bps=0, slippage_bps=0)
    costly = run_strategy_backtest(candles, "breakout", fee_bps=20, slippage_bps=15)

    assert free["out_of_sample"]["trade_count"] == costly["out_of_sample"]["trade_count"] == 1
    assert costly["out_of_sample"]["net_pnl_quote"] < free["out_of_sample"]["net_pnl_quote"]


def test_stop_wins_when_one_candle_touches_both_exit_levels():
    candle = {"open": 100.0, "high": 106.0, "low": 94.0, "close": 101.0}

    assert _resolve_exit(candle, stop_price=98.0, target_price=104.0) == (
        98.0,
        "stop_both_touched",
    )


def test_invalid_or_non_finite_candle_data_is_rejected():
    candles = candle_series()
    candles[80]["close"] = math.nan

    with pytest.raises(BacktestInputError, match="finite"):
        run_strategy_backtest(candles, "breakout")


def test_unknown_strategy_and_unbounded_costs_are_rejected():
    candles = candle_series()

    with pytest.raises(BacktestInputError, match="supported"):
        run_strategy_backtest(candles, "optimised_magic")

    with pytest.raises(BacktestInputError, match="basis points"):
        run_strategy_backtest(candles, "breakout", fee_bps=500)


def test_history_adapter_filters_the_forming_candle(monkeypatch):
    now_ms = int(time.time() * 1000)
    payload = []
    for index in range(121):
        open_time = index * 60_000
        close_time = open_time + 59_999
        payload.append(
            [
                open_time,
                "100",
                "101",
                "99",
                "100.5",
                "10",
                str(close_time),
            ]
        )
    payload[-1][6] = str(now_ms + 60_000)
    requested = []

    def fake_fetch(path, params):
        requested.append((path, params))
        return payload

    monkeypatch.setattr("loki.api.binance_market_data._fetch_json", fake_fetch)
    history = get_binance_spot_history("btcusdt", "1m", 120)

    assert len(history) == 120
    assert history[-1]["time"] < history[0]["time"] + 120 * 60
    assert requested[0][0] == "/api/v3/klines"
    assert requested[0][1]["symbol"] == "BTCUSDT"


def test_history_request_rejects_a_too_short_window():
    with pytest.raises(MarketDataError, match="120"):
        get_binance_spot_history("BTCUSDT", "1h", 99)


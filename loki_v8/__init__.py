"""LOKI v8 — the trading engine behind the AI pages.

    data -> indicators -> strategy / regime -> risk -> backtest

On top of that core sit the pieces the site serves:

* ``live_engine``  — the per-symbol live AI, which follows the backtest's rules bar for bar;
* ``portfolio_ai`` — the same verified rules on 21 markets at once;
* ``daytrade``     — the Day Trader (intraday, flat every night);
* ``agents``       — the Director, Quant and Risk agents behind each live decision;
* ``train``, ``mistake_model``, ``evolution`` — fitting, learning from past trades, weekly self-review;
* ``memory_agent`` — a causal review of the Director's past calls.

The engine produces decisions and tested records. It never places an order: paper
ledgers live in the visitor's browser, and broker relays live in ``loki/api``.
"""

__version__ = "8.8.0"
__all__ = [
    "agents", "backtest", "config", "data", "daytrade", "evolution", "indicators", "live_engine",
    "memory_agent", "mistake_model", "portfolio_ai", "regime", "risk", "strategy", "train",
]

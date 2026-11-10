"""Loki's conversational brain, shared by "Talk to Loki" and the chat room.

No external language model: every answer is assembled from real inputs —
the live AI engine's read for a symbol (the same checklist the paper trader
uses), live public quotes, the shipped 20-year track record, and a plain-
English trading glossary. When the brain has nothing confident to say it
returns None and the caller falls back to its own logic.

Nothing here places orders or gives personal advice.
"""

from __future__ import annotations

import logging
import difflib
import random
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

FOOTER = "Research, not financial advice — Loki never places orders."

# --- symbol detection ---------------------------------------------------------
NAMES: Dict[str, str] = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "ether": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD", "dogecoin": "DOGE-USD", "doge": "DOGE-USD", "ripple": "XRP-USD",
    "xrp": "XRP-USD", "cardano": "ADA-USD", "litecoin": "LTC-USD", "chainlink": "LINK-USD", "bnb": "BNB-USD",
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN", "google": "GOOGL",
    "alphabet": "GOOGL", "meta": "META", "facebook": "META", "tesla": "TSLA", "netflix": "NFLX", "amd": "AMD",
    "jpmorgan": "JPM", "exxon": "XOM", "gold": "GLD", "bonds": "TLT", "treasuries": "TLT",
    "s&p": "SPY", "s&p 500": "SPY", "sp500": "SPY", "the s&p": "SPY", "nasdaq": "QQQ", "dow": "DIA",
    "russell": "IWM", "small caps": "IWM",
}
# Well-known tickers people type in lowercase ("nvda vs amd"). Only symbols
# that are not ordinary English words, so "coin" or "arm" never become tickers.
LOWER_TICKERS = {
    "nvda", "aapl", "msft", "amzn", "googl", "goog", "tsla", "nflx", "avgo", "intc", "pltr", "smci",
    "spy", "qqq", "iwm", "gld", "tlt", "jpm", "xom", "btc", "eth", "uber", "shop", "crwd", "mstr",
}
NOT_TICKERS = {
    "I", "A", "AI", "OK", "US", "USA", "CEO", "IPO", "ETF", "ETFS", "ATR", "RSI", "MACD", "SMA", "EMA", "PNL",
    "THE", "AND", "FOR", "YOU", "ARE", "IS", "IT", "ME", "MY", "DO", "TO", "IN", "ON", "AT", "OF", "OR", "BE",
    "WHAT", "HOW", "WHY", "WHO", "BUY", "SELL", "HOLD", "LOKI", "HI", "HEY", "YES", "NO", "PF", "DD", "VS",
    "FED", "CPI", "GDP", "USD", "EUR", "AUD", "TP", "SL", "FOMO", "YOLO", "LOL", "OMG", "PM", "AM", "UTC", "ET",
}
_PAIR = re.compile(r"\b([A-Za-z]{2,6})\s*[/-]\s*(USDT|USDC|USD)\b", re.I)
_DOLLAR = re.compile(r"\$([A-Za-z]{1,5})\b")
_UPPER = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b")


def detect_symbols(text: str) -> List[str]:
    """Engine-format symbols named in a message (most specific first)."""
    found: List[str] = []

    def add(sym: str) -> None:
        if sym and sym not in found:
            found.append(sym)

    for base, _quote in _PAIR.findall(text):
        add(f"{base.upper()}-USD")
    for sym in _DOLLAR.findall(text):
        add(NAMES.get(sym.lower(), sym.upper()))
    low = f" {text.lower()} "
    for name in sorted(NAMES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            add(NAMES[name])
    for token in _UPPER.findall(text):
        if token not in NOT_TICKERS and not any(token in f for f in found):
            add(NAMES.get(token.lower(), token))
    for word in re.findall(r"(?<![a-z$])[a-z]{2,5}(?![a-z])", text):
        if word in LOWER_TICKERS:
            sym = NAMES.get(word, word.upper())
            if not any(sym in f or f in sym for f in found):
                add(sym)
    # Keep the order the user wrote them in ("nvda vs amd" -> NVDA, AMD).
    def position(sym: str) -> int:
        keys = [sym.lower()] + [n for n, s in NAMES.items() if s == sym]
        hits = [m.start() for k in keys for m in re.finditer(re.escape(k), low)]
        return min(hits) if hits else len(low)
    found.sort(key=position)
    return found[:3]


# Friendly names for quick price answers ("Bitcoin is at …").
LABELS: Dict[str, str] = {
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana", "DOGE-USD": "Dogecoin", "XRP-USD": "XRP",
    "ADA-USD": "Cardano", "LTC-USD": "Litecoin", "LINK-USD": "Chainlink", "BNB-USD": "BNB", "AAPL": "Apple",
    "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "GOOGL": "Alphabet (Google)", "META": "Meta",
    "TSLA": "Tesla", "NFLX": "Netflix", "AMD": "AMD", "JPM": "JPMorgan", "XOM": "Exxon", "GLD": "Gold (GLD)",
    "TLT": "Long-term Treasuries (TLT)", "SPY": "the S&P 500 (SPY)", "QQQ": "the Nasdaq-100 (QQQ)",
    "DIA": "the Dow (DIA)", "IWM": "small caps (IWM)",
}
# Common words that must never be "corrected" into a ticker.
_NOT_TYPOS = {
    "about", "think", "should", "would", "could", "which", "better", "going", "doing", "price", "today", "market",
    "markets", "stock", "stocks", "money", "trade", "trading", "trend", "worth", "right", "there", "their", "where",
    "other", "after", "before", "these", "those", "thing", "things", "apply", "ample", "sample", "metal", "metals",
    "media", "solar", "soldier", "ripples", "tesla's", "google's", "golden", "bitcoins", "ether's", "coins", "crypto",
    "invest", "investing", "nervous", "explain", "please", "thanks", "hello", "loser", "losers", "gainer", "gainers",
    "weather", "whether", "leather", "feather", "together", "either", "neither", "seller", "teller", "apples", "amazing",
    "solid", "salad", "doggo", "ripped", "cards", "chain", "linked", "litre", "ethics", "theory", "nasty", "russia",
    "google", "goggle", "goggles", "alpha", "alphas", "meta's", "binary", "bitter", "tesco", "testa", "taste", "stable",
}
_FUZZY_TARGETS = sorted({k for k in NAMES if len(k) >= 5 and " " not in k} | {"nvidia"})


def fuzzy_symbols(text: str) -> List[Tuple[str, str]]:
    """Ticker guesses for misspelled names ('bitcon' -> BTC-USD), as (typed word, symbol)."""
    out: List[Tuple[str, str]] = []
    for word in re.findall(r"[a-z]{4,12}", text.lower()):
        if word in _NOT_TYPOS or word in NAMES:
            continue
        match = difflib.get_close_matches(word, _FUZZY_TARGETS, n=1, cutoff=0.83)
        if match and abs(len(match[0]) - len(word)) <= 2 and NAMES[match[0]] not in [s for _, s in out]:
            out.append((word, NAMES[match[0]]))
    return out[:2]


# --- glossary -----------------------------------------------------------------
GLOSSARY: Dict[Tuple[str, ...], str] = {
    ("stop loss", "stop-loss", "stoploss", "stop order"): "A stop loss is a pre-set exit price below your entry (for a long). If price falls there, you sell and cap the loss. Loki's AI sets its stop in ATR units — a multiple of the asset's normal daily range — so a jumpy asset gets a wider stop than a calm one. Stops can slip in fast markets or overnight gaps.",
    ("take profit", "profit target", "target price"): "A take-profit is a pre-set exit above entry that banks a gain. Loki's current stock rules deliberately use no fixed target: winners are held until the trend breaks (price closes below its 200-day average), because cutting winners short was the old model's biggest weakness.",
    ("atr", "average true range"): "ATR (Average True Range) is the typical size of a day's price swing, averaged over ~14 days. It's Loki's ruler: stops, targets and the volatility cap are all measured in ATRs so the same rule fits a sleepy ETF and a wild coin.",
    ("rsi", "relative strength index"): "RSI scores recent up-moves against down-moves on a 0-100 scale. Above ~70 is often called overbought, below ~30 oversold. In Loki it is one small input to the momentum factor — never a trigger on its own.",
    ("macd",): "MACD compares a fast and a slow moving average of price. When the fast one rises above the slow one, momentum is turning up. Loki uses MACD as one vote inside its momentum factor.",
    ("moving average", "200-day", "200 day", "sma", "ema"): "A moving average is the average closing price over the last N days. The 200-day average is the classic long-term trend line: above it, the trend is up. Loki's AI only buys above it, and for stocks it exits when price closes back below it.",
    ("drawdown", "max drawdown"): "Drawdown is how far an account falls from its previous peak before recovering. A −20% drawdown means $10,000 dipped to $8,000 at its worst. Loki optimises to keep drawdowns smaller than simply holding — in its 2023-26 simulation its average worst drop was about −21% on stocks versus −27% for buy-and-hold.",
    ("sharpe", "sharpe ratio"): "The Sharpe ratio is return divided by volatility — return per unit of bumpiness. Above 1 is strong for a single strategy. Loki selects its rules on Sharpe (with a drawdown penalty) rather than raw profit, because raw profit rewards luck.",
    ("profit factor",): "Profit factor = total money won ÷ total money lost. Above 1 means the strategy made money; 2 means it won twice what it lost. A high profit factor with few trades is weak evidence, so Loki also reports trade counts.",
    ("win rate", "hit rate"): "Win rate is the share of trades that made money. It's overrated: a strategy winning 35% of the time can be very profitable if winners are big and losers are cut fast — that's how Loki's trend rules work.",
    ("position size", "position sizing", "how much should i buy", "how much to buy"): "Position sizing decides how much to buy. Loki's AI uses volatility targeting: it sizes each position so it swings about 20% a year, capped at 100% of the account — a calm ETF gets a bigger position, a volatile coin a smaller one. That keeps risk steady across assets.",
    ("volatility", "vol target", "volatility targeting"): "Volatility is how much a price jumps around, usually annualised. Volatility targeting sizes positions inversely to it, so every trade carries roughly the same risk. It was the single biggest upgrade over the old AI, which left ~90% of the account idle.",
    ("trend following", "trend-following", "trend"): "Trend following buys what's already rising and steps aside when it stops. It loses small and often in choppy markets but catches big moves and dodges long crashes. Loki's stock AI is a trend follower: strong trend score to enter, 200-day break to exit.",
    ("backtest", "backtesting"): "A backtest replays a strategy on historical prices to see how it would have done. They're easy to fool yourself with, so Loki chooses rules on 2005-2022 only and judges them once on 2023 onward — data the training never touched.",
    ("overfitting", "overfit", "curve fit"): "Overfitting is tuning a strategy so precisely to the past that it fails on new data. Loki fights it by fitting one rulebook across 13 stocks/ETFs (not one per symbol), choosing on the worse of two time windows, and reporting an untouched holdout.",
    ("holdout", "out of sample", "out-of-sample"): "Out-of-sample (holdout) data is history the model never saw while being built. Loki's holdout is 2023 to today; the AI's rules were frozen before looking at it, so its results there are the honest test.",
    ("slippage",): "Slippage is the difference between the price you expected and the price you got. Loki charges 5 basis points (0.05%) of slippage plus 10 bps of fees on every buy and sell in its tests.",
    ("paper trading", "paper trade", "paper money"): "Paper trading means simulated money with real prices — practice with no risk. Loki's AI trading page runs the AI on a paper account in your browser; no real orders are ever sent from it.",
    ("short", "shorting", "short selling"): "Shorting means borrowing and selling an asset to profit if it falls. Loki's paper AI is long-only: in a downtrend it simply sits in cash.",
    ("leverage", "margin"): "Leverage means trading with borrowed money, magnifying both gains and losses. Loki's AI never uses leverage — exposure is capped at 100% of the account.",
    ("diversification", "diversify"): "Diversification is spreading money across assets that don't move together, so one bad bet can't sink the account. Holding several uncorrelated trend positions smooths returns far more than any single one.",
    ("s&p 500", "s&p", "sp500", "the s&p"): "The S&P 500 is an index of about 500 of the largest US companies, weighted by size — the standard yardstick for 'the stock market'. You can't buy the index itself, but funds like SPY track it. Ask 'what about SPY?' and I'll run the AI on it.",
    ("nasdaq", "nasdaq 100", "nasdaq-100"): "The Nasdaq-100 is an index of the 100 largest non-financial companies on the Nasdaq exchange, heavy in tech (Apple, Microsoft, Nvidia…). QQQ is the fund that tracks it. Ask 'what about QQQ?' for the AI's read.",
    ("dow", "dow jones"): "The Dow Jones Industrial Average follows 30 large US companies, weighted by share price rather than size, so it's a narrower gauge than the S&P 500. DIA is the fund that tracks it.",
    ("index fund", "index"): "An index fund simply holds everything in an index (like the S&P 500) instead of picking stocks, which keeps costs low. It's the usual benchmark Loki's AI is compared against: 'buy and hold'.",
    ("etf",): "An ETF (exchange-traded fund) is a basket of assets that trades like one stock — SPY holds the S&P 500, QQQ the Nasdaq-100, GLD gold. Loki's stock AI was trained on 13 major ETFs and stocks.",
    ("bull market", "bear market", "bull", "bear", "regime"): "A bull market is a sustained uptrend, a bear market a sustained downtrend (often −20% or more). Loki classifies every day into STRONG_BULL, BULL, RANGING, BEAR or CRISIS from its trend and volatility, and blocks new buys in bear regimes.",
    ("crypto", "cryptocurrency"): "Crypto trades 24/7 and is roughly 3-4× as volatile as stocks. Loki trains a separate rulebook for it; in its 2023-26 simulation it averaged about +9% a year with a −29% average worst drop, against −71% for just holding coins (hypothetical results).",
    ("expectancy",): "Expectancy is the average profit per trade (wins and losses combined). Positive expectancy after costs is the only thing that makes a strategy worth running.",
    ("candle", "candlestick"): "A candlestick shows one period's open, high, low and close. Green means it closed above the open, red below. Loki's AI decides on completed daily candles only — never on today's still-forming one.",
    ("alpaca",): "Alpaca is a US broker with a free paper-trading API. In Loki's AI trading page you can paste your Alpaca paper keys (they stay in your browser) to see your account and send clicked paper orders.",
    ("dollar cost averaging", "dollar-cost averaging", "dca"): "Dollar-cost averaging means investing a fixed amount on a schedule (say every month) regardless of price. You buy more when prices are low and less when they're high, and it takes the guesswork out of timing. Many long-term investors use it instead of trying to pick the perfect moment.",
    ("market cap", "market capitalization", "market capitalisation"): "Market cap is a company's share price times the number of shares — what the whole company is valued at. Large caps (over ~$10B) like Apple tend to be steadier; small caps move more in both directions.",
    ("dividend", "dividends"): "A dividend is cash a company pays its shareholders out of profits, usually quarterly. Dividend yield is the yearly payout divided by the share price. Loki's backtests use prices adjusted for dividends so returns aren't understated.",
    ("p/e", "pe ratio", "p/e ratio", "price to earnings", "price-to-earnings"): "The P/E ratio is share price divided by earnings per share — how many dollars investors pay for $1 of yearly profit. High P/E means the market expects fast growth (or is optimistic); low P/E can mean value or trouble. Loki's AI is price-based and doesn't use P/E.",
    ("liquidity", "liquid"): "Liquidity is how easily you can buy or sell without moving the price. Big stocks and major coins are very liquid; tiny ones can jump when you trade them. Low liquidity means wider spreads and more slippage.",
    ("compound interest", "compounding", "compound"): "Compounding is earning returns on your previous returns. $10,000 growing 8% a year becomes about $21,600 after 10 years and $46,600 after 20 — the growth speeds up over time. Avoiding big losses matters because a −50% drop needs +100% to recover.",
    ("hodl",): "HODL is crypto slang (from a famous typo of 'hold') for holding through ups and downs instead of trading. Loki's crypto AI does the opposite of HODL in downtrends: it steps aside to cash, which historically cut the worst drops a lot.",
    ("limit order",): "A limit order buys or sells only at your price or better. You control the price but it might not fill. Loki's paper rail uses limit orders for a reviewed entry.",
    ("market order",): "A market order fills immediately at the best available price. It always fills but you can pay a bit more than the last quote (slippage), especially in fast or thin markets.",
    ("stop limit", "stop-limit"): "A stop-limit order becomes a limit order once a trigger price is hit. It protects you from filling at a terrible price, but in a fast drop it may not fill at all.",
    ("trailing stop", "trailing stop loss"): "A trailing stop follows price up and stays a fixed distance below it, locking in gains as a trade works. It sells if price falls back by that distance. Loki's current rules found a trend exit (below the 200-day average) worked better than a tight trail.",
    ("momentum",): "Momentum is the tendency of things that have been rising to keep rising for a while (and fallers to keep falling). Loki's trend score blends momentum with trend, volume flow and volatility.",
    ("stand aside", "standing aside"): "'Stand aside' is the AI's way of saying it would not hold this market right now — usually because its trend score is negative or price is below the 200-day average. It isn't a prediction that price will fall, just that the AI's rules see no uptrend worth owning.",
    ("trend score", "score"): "The trend score is the AI's 0-to-±100 summary of the market: trend, momentum, money flow, squeeze and volatility combined from finished daily candles. Positive means up-trending conditions; the AI only buys when it clears the entry bar (30 for stocks, 8 for crypto) and every other check passes.",
    ("entry bar", "threshold"): "The entry bar is the minimum trend score the AI needs before it will buy: 30 for stocks/ETFs and 8 for crypto. Those bars were chosen on 2005-2022 data and checked on 2023 onward.",
    ("buy setup", "verdict"): "'BUY setup' means every one of the AI's checks passes right now: strong enough trend score, volatility within limits, price above its 200-day average, a regime that allows buying, and it doesn't look like the AI's past losing trades. It's what the paper AI would do — information, not advice.",
    ("support", "resistance"): "Support is a price area where buying has tended to stop declines; resistance is where selling has capped rallies. They're useful for context but fuzzy — Loki's AI doesn't draw them; it uses trend and volatility instead.",
    ("bid", "ask", "spread", "bid-ask"): "The bid is the highest price buyers offer, the ask the lowest price sellers accept; the gap is the spread. You buy at the ask and sell at the bid, so wide spreads are a hidden cost.",
    ("inflation", "cpi"): "Inflation is the rise in prices across the economy, measured by indexes like CPI. High inflation tends to push interest rates up, which can weigh on stocks and bonds. Gold and some commodities have historically held up better.",
    ("interest rate", "interest rates", "fed", "federal reserve", "fomc", "rate cut", "rate hike"): "Interest rates are the price of borrowing money, steered by central banks like the US Federal Reserve. Higher rates usually pressure stock valuations and bond prices; cuts tend to help. Rate decisions are among the biggest scheduled market events — see the News tab.",
    ("recession",): "A recession is a broad economic decline, often defined as two quarters of shrinking GDP. Stocks often fall ahead of or during recessions, which is when trend-following rules that step aside to cash can help most.",
    ("ipo",): "An IPO (initial public offering) is when a private company first sells shares on the stock market. New listings can be very volatile and have little price history — the AI needs about 250 days of history before it will trade one.",
    ("options", "call option", "put option", "calls and puts"): "Options are contracts giving the right (not obligation) to buy (a call) or sell (a put) at a set price before a date. They can magnify gains and losses and can expire worthless. Loki doesn't trade options.",
    ("futures", "futures contract"): "Futures are contracts to buy or sell an asset at a set price on a future date, traded with leverage. Funded-account firms often use futures (e.g. via Tradovate). Leverage means small moves can produce big gains or losses.",
    ("margin call",): "A margin call happens when a leveraged account falls below the broker's required level and you must add money or have positions sold. Loki's AI never uses leverage, so it can't get margin-called.",
    ("risk reward", "risk/reward", "risk to reward", "r multiple"): "Risk/reward compares what a trade could lose (entry to stop) with what it might gain. Trend-following accepts lots of small losses because a few big winners pay for them.",
    ("beta",): "Beta measures how much a stock tends to move with the overall market. Beta 1 moves like the market, 2 moves about twice as much, 0.5 about half.",
    ("alpha",): "Alpha is return above what you'd expect from the market's own move — the 'skill' part. Honest measurement needs costs and out-of-sample data; Loki reports its results against buy-and-hold for that reason.",
    ("yield", "bond yield"): "A yield is the income an investment pays as a percentage of its price — a bond's interest or a stock's dividends. Bond prices fall when yields rise.",
    ("bond", "bonds", "treasury", "treasuries"): "A bond is a loan to a government or company that pays interest and returns the principal at maturity. US Treasuries are seen as the safest. TLT (long-term Treasuries) is one of the markets Loki's AI trades.",
    ("stock", "share", "shares", "equity"): "A stock (or share) is a small piece of ownership in a company. Its price moves with expectations about the company's future profits and the overall market mood.",
    ("fomo",): "FOMO — fear of missing out — is the urge to jump into something because it's already soared. It's one of the most expensive feelings in trading. Rules-based systems like Loki's AI exist partly to take that emotion out.",
    ("fud",): "FUD means fear, uncertainty and doubt — negative talk (sometimes deliberate) that spooks people into selling. Loki's AI ignores headlines and decides only on price behaviour.",
    ("whale", "whales"): "A whale is someone holding or trading enough of an asset to move its price — common talk in crypto.",
    ("altcoin", "altcoins"): "An altcoin is any cryptocurrency other than Bitcoin. Altcoins are usually far more volatile than Bitcoin; Loki's crypto AI sizes them smaller because of that.",
    ("stablecoin", "stablecoins", "usdt", "usdc"): "A stablecoin is a crypto token designed to stay at $1, like USDT or USDC. Traders use them to park cash on crypto exchanges. They carry issuer risk rather than price risk.",
    ("halving",): "The Bitcoin halving cuts the reward for mining new blocks in half about every four years, slowing new supply. Past halvings have been followed by big moves, but with only a handful of examples it's weak evidence.",
    ("breakout",): "A breakout is when price pushes through a level it struggled with before, often on higher volume. Many breakouts fail, which is why Loki's AI waits for a broad trend score rather than one level.",
    ("pullback", "dip", "buy the dip"): "A pullback (or dip) is a temporary drop inside a bigger uptrend. 'Buying the dip' works in strong trends and hurts in real downtrends — the hard part is telling which one you're in. Loki's AI only buys when the broader trend is still up.",
    ("mean reversion",): "Mean reversion is the idea that prices that stretched far from their average tend to snap back. It's the opposite bet to trend following; Loki's Quant lab can test both.",
    ("correlation", "correlated"): "Correlation measures how much two markets move together, from −1 (opposite) to +1 (in lockstep). Combining low-correlation markets is what makes the Portfolio AI smoother than any single market.",
    ("hedge", "hedging"): "Hedging means taking a position that gains if your main bet loses, to reduce risk. It costs something, like insurance. Loki's AI doesn't short or hedge; it reduces risk by holding cash when trends break.",
    ("benchmark", "buy and hold", "buy-and-hold"): "A benchmark is what you compare a strategy against. Loki compares its AI with buy-and-hold — simply owning the same markets the whole time — so you can see if the rules actually add anything.",
    ("tradovate",): "Tradovate is a futures broker used by many funded-account firms. Loki can connect read-only to view your balances and positions — sign in with your Tradovate API credentials and only the issued token is kept, encrypted.",
}


def _glossary(text: str) -> Optional[str]:
    low = text.lower()
    best: Tuple[int, Optional[str]] = (0, None)
    for keys, body in GLOSSARY.items():
        for k in keys:
            if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low) and len(k) > best[0]:
                best = (len(k), body)
    return best[1]


# --- intents ------------------------------------------------------------------
_GREETING = re.compile(r"^\s*(hi|hey|hello|yo|sup|gm|good (morning|afternoon|evening)|howdy|hiya)\b[\s!.,?]*", re.I)
_THANKS = re.compile(r"\b(thanks|thank you|thx|cheers|appreciate it)\b", re.I)
_HELP = re.compile(r"\b(help|what can you do|how do i use|commands|what do you do)\b", re.I)
_IDENTITY = re.compile(r"\b(who are you|what are you|are you (an? )?(ai|bot|human|real))\b", re.I)
_MAKER = re.compile(r"\bwho (made|built|created|makes|owns|runs|designed|coded) (you|loki|this( site| app)?)\b", re.I)
_TIMING = re.compile(r"\b(will (the )?(market|stocks?|crypto) (crash|fall|drop|go (up|down)|recover)|market crash|good time to (invest|buy|get in)|right time to (invest|buy)|time the market|should i (invest|buy|get in) (now|today|yet)|"
                     r"is (it|now) (a )?good (time|moment)|crash (coming|soon)|is (this|it) a bubble|recession (coming|soon))\b", re.I)
_TRACK = re.compile(r"\b(track record|historic\w*|history|backtest(ed|s)?|profitab\w*|how (good|well)|how did|performance|results?|win rate|returns?|proof|make money|making money|money|earn\w*)\b", re.I)
_HOW_AI = re.compile(r"\b(how (does|do) (the |your )?(ai|you|loki|model) (work|trade|decide|think|learn)|learn(s|ing)? from (its |your )?mistakes|explain (the |your )?(ai|model))\b", re.I)
_ORDER = re.compile(r"^\s*(please\s+)?(buy|sell|short|close|place|submit|execute)\b", re.I)
_MARKET = re.compile(r"\b(market(s)? (today|now|doing|look)|how('s| is) the market|market overview|market update|the tape|"
                     r"(is|are) (the )?(stock )?markets? (up|down|open|closed|red|green)|stocks? (are )?(up|down) today)\b", re.I)
_MOVERS = re.compile(r"\b(what('s| is) moving|movers?|gainers?|losers?|biggest (moves?|winners?|losers?|jumps?|drops?)|top (stocks|coins|performers)|what('s| is) (hot|pumping|dumping))\b", re.I)
_ADVICE = re.compile(r"\b(should i|can i|is it (a )?good (time|idea)|worth (buying|it)|buy|sell|invest|entry|enter|think of|thoughts on|opinion|analy[sz]e|read on|outlook|what about|look(s|ing)? (at|good)|forecast|predict|"
                     r"tell me about|how('s| is) .{1,20} doing|price|news on|update on|vs\.?|versus|compare|better|which|going (up|down))\b", re.I)
_ASK_AI_DEFINE = re.compile(r"\b(what('s| is| are| does)|explain|define|meaning of|means?|tell me about|why)\b", re.I)


def _pick(options: List[str]) -> str:
    return random.choice(options)


# --- data access (lazy imports keep startup light; tests patch these) ---------
def _ai_read(symbol: str) -> Optional[Dict[str, Any]]:
    from fastapi import HTTPException

    from loki.api import ai_paper

    try:
        analysis = ai_paper._analyze(symbol)
    except HTTPException:
        return None
    except Exception as exc:  # noqa: BLE001 - chat must never 500 on a data blip
        logger.warning("brain AI read failed for %s: %s", symbol, exc.__class__.__name__)
        return None
    cfg = ai_paper._get_pipeline(symbol)[1]
    return {"analysis": analysis, "cfg": cfg, "source": ai_paper._PROFILE_SOURCE.get(symbol, "default")}


def _quotes(symbols: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    from loki.api.market_feed import quote_or_failure

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
        return list(pool.map(lambda s: quote_or_failure(s[0], s[1], 15.0), symbols))


def _portfolio() -> Optional[Dict[str, Any]]:
    from fastapi import Response

    from loki.api import ai_paper

    try:
        portfolio: Optional[Dict[str, Any]] = ai_paper.ai_portfolio(Response())
        return portfolio
    except Exception as exc:  # noqa: BLE001 - chat must never 500 on a data blip
        logger.warning("brain portfolio read failed: %s", exc.__class__.__name__)
        return None


def _universal() -> Dict[str, Any]:
    from loki_v8.train import load_report

    return (load_report() or {}).get("universal") or {}


# --- answer builders ----------------------------------------------------------
def _pct(v: Any) -> str:
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _symbol_record(symbol: str) -> Optional[str]:
    uni = _universal()
    for kind in ("equity", "crypto"):
        for row in (uni.get(kind) or {}).get("holdout") or []:
            if row.get("symbol") == symbol and row.get("strategy"):
                s, b = row["strategy"], row.get("buy_hold") or {}
                return (f"In a simulation on held-back 2023→now data (hypothetical), the AI's rules made {_pct(s.get('cagr_pct'))} a year on {symbol} "
                        f"over {s.get('trades')} trades (worst drop {_pct(s.get('max_dd_pct'))}), versus {_pct(b.get('cagr_pct'))} "
                        f"a year and {_pct(b.get('max_dd_pct'))} worst drop for simply holding it.")
    return None


GATE_TEXT = {
    "signal": ("the signal is strong enough", "the signal isn't strong enough yet"),
    "volatility_cap": ("volatility is within limits", "volatility is above the cap"),
    "trend_filter": ("price is above its 200-day average (uptrend)", "price is below its 200-day average (no uptrend)"),
    "regime_filter": ("the market regime allows buying", "it's in a bear regime, so new buys are blocked"),
    "learned_filter": ("it doesn't look like the AI's past losing setups", "it looks too much like the AI's past losing trades"),
}


def explain_symbol(symbol: str, name: str = "") -> Optional[str]:
    read = _ai_read(symbol)
    if not read:
        return None  # caller falls back to its own quote/headline answer
    a, cfg = read["analysis"], read["cfg"]
    plan = a.get("plan") or {}
    regime = (a.get("regime") or "unknown").replace("_", " ").lower()
    lines = [f"Here's the AI's live read on {symbol} at {a['price']:,.2f}:"]
    if not plan.get("ready"):
        lines.append(f"It needs about {plan.get('bars_needed', 251)} days of history before it will trade this one, and only has {plan.get('bars', 0)}.")
    else:
        score, thr = plan.get("score"), plan.get("threshold")
        lines.append(f"• Market regime: {regime}. Trend score {score:+.0f} against an entry bar of {thr:.0f}.")
        if "bull" in regime and score is not None and score < 0:
            lines.append("  (The longer-term regime is still up, but short-term momentum has turned down.)")
        elif regime in ("bear", "crisis") and score is not None and score > 0:
            lines.append("  (Short-term momentum is improving, but the longer-term regime is still down.)")
        passed = [GATE_TEXT.get(g["name"], (g["name"], g["name"]))[0] for g in plan.get("gates", []) if g["passed"]]
        failed = [GATE_TEXT.get(g["name"], (g["name"], g["name"]))[1] for g in plan.get("gates", []) if not g["passed"]]
        if plan.get("action") == "SELL":
            failed = ["its trend score is negative" if f == GATE_TEXT["signal"][1] else f for f in failed]
        if plan.get("allowed"):
            vol = plan.get("ann_vol") or 0
            exposure = min(cfg.max_exposure_pct, cfg.target_vol_pct / vol * 100) if vol > 0 else None
            lines.append("• Verdict: BUY setup — every check passes (" + "; ".join(passed) + ").")
            if exposure:
                lines.append(f"• It would put about {exposure:.0f}% of a paper account in, with a protective stop "
                             f"{cfg.stop_loss_atr:g} ATR below entry" + (", holding until price breaks its 200-day average." if cfg.exit_mode == "trend" else "."))
        else:
            verdict = "WAIT — it would not buy right now"
            if plan.get("action") == "SELL":
                verdict = "STAND ASIDE — momentum is pointing down"
            lines.append(f"• Verdict: {verdict}, because " + "; and ".join(failed) + ".")
            if passed:
                lines.append("• On the plus side: " + "; ".join(passed) + ".")
        wp = plan.get("win_probability")
        if wp is not None:
            lines.append(f"• Its learned-from-mistakes filter says setups like today's won about {wp * 100:.0f}% of the time historically.")
    record = _symbol_record(symbol)
    if record:
        lines.append(record)
    if read["source"] == "universal_long_history":
        lines.append("These rules were trained on up to 20 years of history and checked on data they never saw.")
    lines.append(FOOTER)
    return "\n".join(lines)


def market_overview() -> str:
    basket = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("BTC/USDT", "Bitcoin"), ("ETH/USDT", "Ethereum"),
              ("GC=F", "Gold"), ("^VIX", "VIX (fear gauge)"), ("CL=F", "Oil")]
    rows = [r for r in _quotes(basket) if r.get("ok")]
    if not rows:
        return "The market feeds aren't answering right now — give it a minute and ask again."
    parts = [f"{r['label']} {r['price']:,.2f} ({_pct(r.get('change_pct'))})" for r in rows]
    movers = sorted([r for r in rows if isinstance(r.get("change_pct"), (int, float))], key=lambda r: abs(r["change_pct"]), reverse=True)
    lead = ""
    if movers:
        m = movers[0]
        lead = f" Biggest mover in this basket: {m['label']} at {_pct(m['change_pct'])}."
    vix = next((r for r in rows if r["symbol"] == "^VIX"), None)
    mood = ""
    if vix:
        mood = (" Fear is elevated (VIX above 25)." if vix["price"] > 25 else
                " Markets are calm (VIX under 15)." if vix["price"] < 15 else " Fear is moderate.")
    return ("Quick market check: " + " · ".join(parts) + "." + lead + mood +
            " Open Live markets for the full board, or name any ticker and I'll run the AI on it.\n" + FOOTER)


def _years_sentence() -> str:
    years = (_universal().get("portfolio") or {}).get("calendar_years") or []
    full = [y for y in years if not y.get("partial")]
    if not full:
        return ""
    losers = [y for y in full if y["return_pct"] < 0]
    worst = ("; losing years: " + ", ".join(f"{y['year']} {_pct(y['return_pct'])}" for y in losers)) if losers else ""
    return (f"Year by year, the simulated Portfolio AI was up in {len(full) - len(losers)} of {len(full)} full years "
            f"({full[0]['year']}-{full[-1]['year']}){worst}. No strategy can promise a profit every year, or every month.")


def _hindsight_sentence() -> str:
    hc = (_universal().get("portfolio") or {}).get("hindsight_check") or {}
    h, b = hc.get("holdout") or {}, hc.get("holdout_buy_hold") or {}
    if not h or not b:
        return ""
    return ("Honesty check: part of that record comes from stocks that are known winners today (Apple, Nvidia and so on). "
            f"On markets nobody had to pick with hindsight — index funds, gold, bonds, Bitcoin and Ether — the same method made "
            f"{_pct(h.get('cagr_pct'))} a year (Sharpe {float(h.get('sharpe') or 0):.2f}, worst drop {_pct(h.get('max_dd_pct'))}) against "
            f"{_pct(b.get('cagr_pct'))} for holding them (Sharpe {float(b.get('sharpe') or 0):.2f}, worst drop {_pct(b.get('max_dd_pct'))}): "
            f"it {hc.get('verdict', 'is comparable')}. So its real strength is protection in bad markets, not beating the market.")


def track_record() -> str:
    uni = _universal()
    eq, cr = (uni.get("equity") or {}), (uni.get("crypto") or {})
    if not eq and not cr:
        return "The 20-year track record isn't loaded on this server yet."
    out = ["Simulated (hypothetical) results. The AI's rules were chosen using 2005-2022 only, then tested once on 2023→now — data they never saw — with assumed fees and slippage:"]
    for label, b in (("Stocks/ETFs", eq), ("Crypto", cr)):
        p = b.get("holdout_pooled") or {}
        if p:
            out.append(f"• {label}: {_pct(p.get('mean_cagr_pct'))} a year on average, profitable on {p.get('profitable_symbols')} of "
                       f"{p.get('symbols')} symbols, average worst drop {_pct(p.get('mean_max_dd_pct'))}. Simply holding the same "
                       f"assets: {_pct(p.get('buy_hold_mean_cagr_pct'))} a year with {_pct(p.get('buy_hold_mean_max_dd_pct'))} worst drop.")
    out.append("In that simulation it was profitable on data held back from training, with smaller drawdowns than holding, but it trailed buy-and-hold in the strong 2023-26 bull run. These are hypothetical results, not real trading: simulations have limits and future results can differ.")
    for extra in (_years_sentence(), _hindsight_sentence()):
        if extra:
            out.append(extra)
    return "\n".join(out)


def how_ai_works() -> str:
    uni = _universal()
    meta = ((uni.get("equity") or {}).get("learning_from_mistakes") or {})
    learn = ("It also learns from its mistakes: I replayed its 20 years of trades, recorded the conditions at every entry, and trained a "
             f"model on which setups turned into losses. It now skips setups that historically won less than {round(meta.get('threshold', 0.3) * 100)}% "
             f"of the time — learned from {meta.get('lessons_final', 'hundreds of')} past trades, and kept only because it also held up on unseen data."
             if meta.get("kept") else
             "It has a learn-from-mistakes filter trained on its own past losing trades; it's only switched on where it improved unseen results.")
    return ("How the AI trades, step by step:\n"
            "1. Every day it scores each market on trend, momentum, money flow, squeeze and volatility, using only finished candles.\n"
            "2. It only buys when the score clears a high bar AND price is above its 200-day average AND the regime isn't bearish AND volatility isn't extreme.\n"
            "3. It sizes the position so it swings about 20% a year (never more than 100% of the account), and sets a wide protective stop.\n"
            "4. Stocks: it holds until price closes below the 200-day average. Crypto: it exits when the signal turns to SELL.\n"
            f"5. {learn}\n"
            "Ask 'how did the AI do?' for its track record, or name any ticker for a live read.\n" + FOOTER)


def portfolio_answer() -> str:
    uni = _universal().get("portfolio") or {}
    h, b = uni.get("holdout") or {}, uni.get("holdout_buy_hold") or {}
    lines = ["The Portfolio AI runs the verified strategy on 21 markets at once — 13 stocks/ETFs (including gold and bonds) "
             "and 8 coins — shares capital equally, scales the whole book to a steady risk level, and never holds more than 100% of the account."]
    if h:
        lines.append(f"In a simulation on held-back 2023→now data (hypothetical, not real trading) it made {_pct(h.get('cagr_pct'))} a year with a Sharpe of {float(h.get('sharpe') or 0):.2f} and a worst drop of "
                     f"{_pct(h.get('max_dd_pct'))}; holding all 21 equally made {_pct(b.get('cagr_pct'))} a year but fell {_pct(b.get('max_dd_pct'))} at worst.")
    plan = _portfolio()
    if plan and plan.get("health", {}).get("status") not in (None, "unknown"):
        h = plan["health"]
        lines.append(f"Self-check: over the last 12 months it had a Sharpe of {h.get('last_year_sharpe')} — better than "
                     f"{round(h.get('percentile_vs_history') or 0)}% of its 2005-2022 years, so it is {h.get('detail')}.")
    if plan and plan.get("changes"):
        moves = ", ".join(f"{c['action']} {c['symbol']} ({c['date']})" for c in plan["changes"][:5])
        lines.append(f"Recent moves: {moves}.")
    for extra in (_years_sentence(), _hindsight_sentence()):
        if extra:
            lines.append(extra)
    levels = [lv for lv in uni.get("risk_levels") or [] if lv.get("whole_record")]
    if len(levels) > 1:
        lines.append("You can pick a risk level on the AI trading page (same rules, simulated 2005→now): " + "; ".join(
            f"{lv['label']} {_pct(lv['whole_record'].get('cagr_pct'))} a year with a worst drop of {_pct(lv['whole_record'].get('max_dd_pct'))}"
            for lv in levels) + ". Steady is the default because it earned the most for the risk it took; higher levels mean bigger drops.")
    park = uni.get("cash_parking") or {}
    if park.get("symbol"):
        lines.append(f"Cash it isn't using (about {round(park.get('average_cash', 0) * 100)}% on average) sits in Treasury bills ({park['symbol']}), "
                     "so idle money still earns interest — that's included in the numbers above.")
    ci = uni.get("holdout_ci") or {}
    if ci.get("cagr_pct"):
        lines.append(f"Honest uncertainty: that simulated yearly return could plausibly have been anywhere from "
                     f"{_pct(ci['cagr_pct'][0])} to {_pct(ci['cagr_pct'][1])} by luck alone.")
    if plan and plan.get("positions"):
        held = [p for p in plan["positions"] if p.get("weight", 0) > 0]
        if held:
            lines.append(f"Right now (as of {plan.get('as_of')}) it holds " +
                         ", ".join(f"{p['symbol']} {p['weight'] * 100:.1f}%" for p in held[:8]) +
                         f", with {plan.get('cash', 0) * 100:.0f}% in cash.")
        else:
            lines.append("Right now every market is out of its uptrend, so it's fully in cash.")
    lines.append("Start it with paper money on the AI trading page.\n" + FOOTER)
    return "\n".join(lines)


def timing_answer() -> str:
    """'Is now a good time to invest?' — no prediction, just what the data and the AI show."""
    lines = ["Nobody can reliably time the market, and I won't pretend to. Here's what the data and the AI show right now:",
             market_overview().replace("\n" + FOOTER, "")]
    plan = _portfolio()
    if plan and isinstance(plan.get("cash"), (int, float)):
        invested = max(0.0, 1.0 - float(plan["cash"])) * 100
        lines.append(f"The Portfolio AI, which follows trends across 21 markets, currently has about {invested:.0f}% invested and "
                     f"{float(plan['cash']) * 100:.0f}% in cash — " +
                     ("it sees plenty of markets in uptrends." if invested >= 60 else
                      "it sees a mixed picture." if invested >= 25 else "few markets are in uptrends, so it's mostly waiting."))
    lines.append("Many long-term investors skip timing altogether and invest a fixed amount on a schedule. Name any ticker for the AI's live read.\n" + FOOTER)
    return "\n".join(lines)


def maker_answer() -> str:
    from loki.api import site_access

    return (f"Loki was built and is run by {site_access.operator_name()}. I'm the workspace's built-in assistant: I explain live market data "
            "and the trading AI's decisions. I'm not a general chatbot and I never place orders. The Transparency page has contact details.")


_EVOLVE = re.compile(r"\b(evolv\w*|self.?(improv\w*|learn\w*|evolv\w*)|improv\w* (itself|over time|on its own)|get(ting)? (better|smarter)|"
                     r"re-?train\w*|generations?|champion|challengers?|adapt\w*|upgrad\w* itself)\b", re.I)


def _evolution() -> Dict[str, Any]:
    from loki.api import ai_paper

    try:
        return ai_paper.evolution_summary()
    except Exception as exc:  # noqa: BLE001 - chat must never 500
        logger.warning("brain evolution read failed: %s", exc.__class__.__name__)
        return {}


def evolution_answer() -> str:
    data = _evolution()
    latest = data.get("latest")
    lines = ["Yes — the AI re-tests itself every week. The live version (the champion) competes against challenger versions of "
             "itself on the latest 20 years of data. A challenger only takes over if it's clearly better on the same tests the "
             "champion was chosen with and no worse on recent markets, so it can't 'improve' by fitting last month's noise. "
             "Its learn-from-mistakes filter is also retrained on every closed trade, including the newest."]
    if latest:
        for kind, label in (("equity", "Stocks/ETFs"), ("crypto", "Crypto")):
            k = latest.get(kind) or {}
            m = k.get("mistakes") or {}
            outcome = "a challenger was promoted" if k.get("promoted") else "the champion kept its place"
            filt = (f"; its mistake filter retrained on {m.get('lessons')} closed trades" if m.get("kept") and m.get("lessons") else "")
            lines.append(f"• {label}: {outcome} after {k.get('challengers_tested', 0)} challengers{filt}.")
        lines.append(f"That was generation {latest.get('generation')} ({latest.get('date')}); {data.get('promotions', 0)} promotions so far. "
                     "The full log is on the AI trading page. Results are hypothetical backtests.")
    else:
        lines.append("The first weekly generation hasn't been recorded on this server yet.")
    lines.append(FOOTER)
    return "\n".join(lines)


_PORTFOLIO_Q = re.compile(r"\b(portfolio ai|portfolio|all (the )?markets|what (is|are) (the ai|you|it) (holding|buying|in)|holdings|allocation|what changed|this week|how is the ai doing|ai doing|self.?check|health)\b", re.I)


# --- conversation, boundaries and practical questions ---------------------------
_SMALLTALK = re.compile(r"^\s*(how are (you|u)|how('s| is) it going|how you doing|what'?s up|wh?at'?s good|sup|you good|how('s| is) your day)\b", re.I)
_LAUGH = re.compile(r"^\s*(lol+|lmao+|haha+|hehe+|rofl|😂|🤣|nice|cool|ok(ay)?|k|great|awesome|wow)[\s!.?]*$", re.I)
_BYE = re.compile(r"^\s*(bye+|goodbye|good night|goodnight|see (you|ya)|cya|later|gtg|gotta go|night)\b", re.I)
_JOKE = re.compile(r"\b(tell me a joke|joke|make me laugh|something funny)\b", re.I)
_OFFTOPIC = re.compile(r"\b(weather|forecast for (today|tomorrow)|recipe|movie|song|sports?|football|soccer|basketball|homework|poem|girlfriend|boyfriend)\b", re.I)
_FEELINGS = re.compile(r"\b(nervous|scared|afraid|worried|anxious|stress(ed)?|panic\w*|freaking out|lost (a lot of |so much |some )?money|losing money|down bad|wiped out|regret)\b", re.I)
_TIPS = re.compile(r"\b(what should i (buy|invest in|trade)|stock tips?|give me a tip|any tips|best (stock|coin|crypto|investment)s?|make me (rich|money)|get rich|"
                   r"how much (should|do) i (invest|put|buy)|i have \$?\d[\d,]*|what to buy|trade for me|manage my money|pick (a|me a) stock)\b", re.I)
_TRUST = re.compile(r"\b(trust|risky|risk|safe|worst (drop|loss|case)|lose money|guarantee\w*|reliable|scam|legit)\b", re.I)
_PRIVACY = re.compile(r"\b(my data|data (safe|secure|private)|personal (data|info\w*)|store (my|any)|privacy|delete (my|account)|cookies?|track(ing)? me|sell my data)\b", re.I)
_ADVICE_Q = re.compile(r"\b(financial advi[cs]e|is this advice|are you (an? )?(advisor|adviser|licensed)|licensed|regulated)\b", re.I)
_START = re.compile(r"\b(how do i (start|begin|use (this|loki|the paper|it))|get(ting)? started|where (do|should) i (start|begin)|i'?m new|new here|beginners?|first time|strateg(y|ies) for (a )?(beginner|newbie|new)|good strategy)\b", re.I)
_BROKER = re.compile(r"\b(connect (to )?(my )?(broker|account|exchange)|real money|live (trading|account)|link my (broker|account))\b", re.I)
_GAME = re.compile(r"\b(game|bored|trivia|wordle)\b", re.I)
_PRICE_ONLY = re.compile(r"\b(price|quote|trading at|how much is|worth|cost)\b", re.I)
_ANALYSIS_WORDS = re.compile(r"\b(should|buy|sell|think|analy[sz]e|ai|read|good|bad|outlook|vs|versus|compare|better|predict|forecast)\b", re.I)
_FOLLOWUP = re.compile(r"\b(it|its|it's|that one|this one|that|them)\b", re.I)

JOKES = [
    "Why did the trader stand aside in the rain? Because the trend score said no umbrella was worth the risk.",
    "I asked the market for a sign. It gave me a candlestick… then a gap down.",
    "My portfolio and I have a lot in common: we both look better on a long-term chart.",
    "A bull, a bear and a trend follower walk into a bar. The trend follower waits for the bar to close.",
]


def _who(name: str) -> str:
    return f", {name}" if name else ""


def quote_answer(symbol: str) -> Optional[str]:
    label = LABELS.get(symbol, symbol)
    rows = [r for r in _quotes([(symbol, label)]) if r.get("ok") and isinstance(r.get("price"), (int, float))]
    if not rows:
        return None
    r = rows[0]
    change = f", {_pct(r.get('change_pct'))} today" if isinstance(r.get("change_pct"), (int, float)) else ""
    return (f"{label} is at {r['price']:,.2f}{change} (public feed, can be slightly delayed). "
            f"Want the AI's read on it? Ask 'what about {symbol.replace('-USD', '')}?'")


def _board() -> Optional[Dict[str, Any]]:
    from fastapi import Response

    from loki.api import live_markets

    try:
        board: Optional[Dict[str, Any]] = live_markets.live_board(Response())
        return board
    except Exception as exc:  # noqa: BLE001 - chat must never 500
        logger.warning("brain board read failed: %s", exc.__class__.__name__)
        return None


def movers_answer() -> str:
    board = _board() or {}
    m = board.get("movers") or {}
    fmt = lambda rows: ", ".join(f"{r.get('label') or r['symbol']} {_pct(r.get('change_pct'))}" for r in rows)  # noqa: E731
    if not (m.get("gainers") or m.get("losers")):
        return market_overview()
    parts = ["Biggest moves on the live board right now:"]
    if m.get("gainers"):
        parts.append(f"• Leading: {fmt(m['gainers'])}.")
    if m.get("losers"):
        parts.append(f"• Lagging: {fmt(m['losers'])}.")
    parts.append("Name any of them and I'll run the AI on it, or open Live markets for candlestick charts.\n" + FOOTER)
    return "\n".join(parts)


def trust_answer() -> str:
    uni = _universal()
    eq = (uni.get("equity") or {}).get("holdout_pooled") or {}
    cr = (uni.get("crypto") or {}).get("holdout_pooled") or {}
    pf = (uni.get("portfolio") or {}).get("holdout") or {}
    lines = ["Fair question — here's the honest picture, not a sales pitch:",
             "• All results are hypothetical backtests with fees and slippage, chosen on 2005-2022 and tested on 2023 onward. They are not real trading and can't promise anything about the future."]
    if eq:
        lines.append(f"• Stocks/ETFs: average worst drop {_pct(eq.get('mean_max_dd_pct'))} on 2023-onward data, versus {_pct(eq.get('buy_hold_mean_max_dd_pct'))} for simply holding.")
    if cr:
        lines.append(f"• Crypto: average worst drop {_pct(cr.get('mean_max_dd_pct'))}, versus {_pct(cr.get('buy_hold_mean_max_dd_pct'))} for holding.")
    if pf:
        lines.append(f"• Portfolio AI (21 markets): worst drop {_pct(pf.get('max_dd_pct'))} on 2023-onward data.")
    lines.append("• It loses on many individual trades and trails buy-and-hold in strong bull runs; its edge is avoiding the deepest drops.")
    years = _years_sentence()
    if years:
        lines.append("• " + years)
    lines.append("The safest way to judge it is to run it on paper money first — the AI trading page does exactly that.\n" + FOOTER)
    return "\n".join(lines)


def tips_answer(name: str) -> str:
    lines = [f"I can't pick investments for you{_who(name)} — I don't know your goals, timeline or situation, and nobody can promise returns. "
             "What I can do is show you exactly what the AI sees:"]
    plan = _portfolio()
    held = [p for p in (plan or {}).get("positions", []) if p.get("weight", 0) > 0]
    if held:
        lines.append("• Right now the Portfolio AI (paper) holds " + ", ".join(p["symbol"] for p in held[:8]) +
                     " — markets where its trend rules currently see an uptrend.")
    lines += ["• Ask 'what about NVDA?' (or any ticker) for the AI's live checklist on one market.",
              "• Many people start with broad index funds and invest a fixed amount on a schedule (dollar-cost averaging), and only risk money they can afford to lose.",
              "• Try any idea on paper money first — the AI trading page has a paper account.\n" + FOOTER]
    return "\n".join(lines)


def feelings_answer(name: str) -> str:
    return (f"That's a really normal feeling{_who(name)} — markets are stressful, and losses sting. A few things that genuinely help:\n"
            "• Size positions so a bad day can't hurt you badly — Loki's AI aims for each position to swing about 20% a year, never more than the whole account.\n"
            "• Decide your exit before you enter (a stop), so you're not deciding in a panic.\n"
            "• Step back from the screen; the AI only looks at finished daily candles for a reason.\n"
            "• Practise on paper money until the process feels boring.\n"
            "If money worries are weighing on you, talking to someone you trust or a licensed financial counsellor can help. "
            "Want me to check how the market is doing right now?")


PRIVACY_TEXT = ("Here's what Loki keeps:\n"
                "• Your nickname, watchlist, alerts, chat history and paper trades are stored in your own browser (you can clear them any time).\n"
                "• If you create an account, your email, nickname and saved workspace are stored so you can sign in on other devices — you can export or delete everything from My account.\n"
                "• Messages in Loki's private room are kept for up to 30 days. Broker tokens are encrypted, and Loki never asks for your broker password.\n"
                "• No advertising trackers and no selling of data. The full details are on the Privacy and Cookies pages.")
ADVICE_TEXT = ("No — Loki is an information and research tool, not a financial adviser, and nothing here is personal financial advice. "
               "The AI's reads describe what its rules see in the data; results shown are hypothetical. Decisions with real money are yours, "
               "and a licensed adviser can help with your specific situation.")
START_TEXT = ("Welcome! The quickest way in:\n"
              "1. Ask me 'how's the market?' or name any ticker ('what about AAPL?') to see the AI's live read.\n"
              "2. Open Live markets for candlestick charts — pick any tile, switch timeframes, hover for prices.\n"
              "3. Go to AI trading and press 'Start Portfolio AI' to run the 21-market AI on paper money.\n"
              "4. Explore Quant lab to test your own ideas on history, or the News tab for what's moving markets.\n"
              "Everything uses paper money unless you deliberately connect a broker.")
BROKER_TEXT = ("You can connect two kinds of accounts:\n"
               "• Alpaca (paper): paste your Alpaca paper API keys on the AI trading page — they stay in your browser — to see your account and send orders you click yourself.\n"
               "• Tradovate (futures, read-only): sign in with your Tradovate API credentials to view balances and positions; only the issued token is kept, encrypted.\n"
               "Loki never places real-money trades on its own, and chat can't place orders.")
GAME_TEXT = ("The 'Bored?' tab at the bottom of the menu has a quick five-letter word game and rare-answer trivia — a mental reset between market checks.")
OFFTOPIC_TEXT = ("I stick to markets — I'd only be guessing about that. Ask me about any stock or coin, how the market's doing, "
                 "what a trading term means, or how the AI works.")
FALLBACK_TEXT = ("I didn't quite catch that{who}. I'm best at: a ticker or coin ('what about Tesla?'), 'how's the market?', "
                 "'biggest movers today', a trading term ('what is a stop loss?'), or 'how does the AI work?'.")


def order_answer(name: str, symbols: List[str]) -> str:
    target = f" on {symbols[0]}" if symbols else ""
    return (f"I can't place orders{_who(name)} — chat never trades, by design. I can show you the AI's live read{target}, "
            "and you can practise the trade with paper money on the AI trading page, or place it yourself through a broker you connect.")


# --- prediction-market odds (Polymarket, read-only) ------------------------------
_ODDS = re.compile(r"\b(odds|chances?|probability|probabilities|polymarket|prediction markets?|betting|bet on|crowd (think|says?|expects?)|"
                   r"will the fed|fed (decision|meeting)|rate (cut|hike)s?|recession|ipo)\b", re.I)
_ODDS_FILLER = re.compile(r"\b(what('s| is| are)?|whats|the|of|a|an|on|for|to|is|are|be|there|any|odds|chances?|probability|"
                          r"probabilities|polymarket|prediction|markets?|betting|bet|crowd|think|says?|expects?|do|does|"
                          r"people|traders|right now|currently|loki|please|tell|me|about|how|likely|will|that)\b", re.I)


def _poly_search(query: str) -> List[Dict[str, Any]]:
    from loki.api import polymarket

    try:
        return polymarket.search(query, 4)
    except Exception as exc:  # noqa: BLE001 - chat must never 500
        logger.warning("brain polymarket search failed: %s", exc.__class__.__name__)
        return []


def odds_answer(msg: str) -> Optional[str]:
    query = " ".join(_ODDS_FILLER.sub(" ", msg).split())[:60]
    month = re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", msg, re.I)
    if re.search(r"\b(fed|rates?)\b", msg, re.I) and month:
        query = f"fed decision {month.group(1).lower()}"  # Polymarket titles these "Fed Decision in <Month>?"
    elif not query and re.search(r"fed|rate", msg, re.I):
        query = "fed decision"
    if not query:
        return None
    found = _poly_search(query)
    if not found:
        return None
    words = {w for w in re.findall(r"[a-z0-9]+", _ODDS_FILLER.sub(" ", f"{msg} {query}").lower()) if len(w) > 2}
    names_country = re.search(r"\b(japan|uk|britain|china|europe|eu|canada|australia|germany|france|india)\b", msg, re.I)

    def relevance(event: Dict[str, Any]) -> Tuple[int, int]:
        title = event["title"].lower()
        overlap = sum(1 for w in words if w in title)
        us_bias = 1 if not names_country and re.search(r"\b(us|u\.s\.|fed)\b", title) else 0
        return overlap, us_bias

    found = sorted(found, key=relevance, reverse=True)  # stable: ties keep Polymarket's ranking
    event = found[0]
    end = str(event.get("end") or "")[:10]
    top = event.get("markets", [])[:4]
    if len(top) == 1:
        odds = f"{top[0]['yes'] * 100:.0f}% chance of Yes"
    else:
        odds = " · ".join(f"{m['label']} {m['yes'] * 100:.1f}%" for m in top)
    lines = [f"Polymarket traders currently price “{event['title']}”{f' (resolves {end})' if end else ''}: {odds}."]
    if len(found) > 1:
        lines.append("Related: " + "; ".join(e["title"] for e in found[1:3]) + ".")
    lines.append("These are crowd betting odds, not Loki's prediction. The Prediction markets tab has the full board, "
                 "Crowd vs Machine, and paper predictions.")
    return "\n".join(lines)


HELP_TEXT = ("Here's what I can do:\n"
             "• Name any ticker or coin — 'what about NVDA?', 'should I buy bitcoin?' — and I'll run the live AI on it and explain its decision. Compare two with 'NVDA vs AMD'.\n"
             "• 'How's the market?' for a quick live check across stocks, crypto, gold and the fear gauge.\n"
             "• 'How did the AI do?' for its honest 20-year track record, or 'how does the AI work?'.\n"
             "• 'What is the Portfolio AI holding?' — the 21-market book and its unseen-data record.\n"
             "• 'What are the odds of a Fed rate cut?' — live crowd odds from Polymarket prediction markets.\n"
             "• Ask what any trading term means — stop loss, ATR, drawdown, Sharpe, position sizing, trend following…\n"
             "I don't place orders or give personal financial advice — I explain what the data and the AI are saying.")


def answer(text: str, name: str = "", *, allow_data: bool = True, skip: Tuple[str, ...] = (),
           order_reply: bool = False, last_symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Best answer for a message, or None to let the caller fall back.

    ``skip`` names intents the caller answers better itself (e.g. the chat
    room keeps its own greeting). Order commands always fall through so the
    caller's order-refusal path answers them.
    """
    msg = (text or "").strip()
    if not msg:
        return None
    if _ORDER.match(msg):
        if not order_reply:
            return None
        return {"intent": "order", "text": order_answer(name, detect_symbols(msg))}
    result = _answer(msg, name, allow_data, last_symbol)
    if result and result["intent"] in skip:
        return None
    return result


def fallback(name: str = "") -> Dict[str, Any]:
    """Friendly catch-all for messages nothing else could ground."""
    return {"intent": "fallback", "text": FALLBACK_TEXT.format(who=_who(name))}


def _answer(msg: str, name: str, allow_data: bool, last_symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    who = f", {name}" if name else ""
    symbols = detect_symbols(msg)
    low = msg.lower()
    read_as = ""
    if _OFFTOPIC.search(msg) and not symbols:
        return {"intent": "off_topic", "text": OFFTOPIC_TEXT}
    if not symbols:
        guesses = fuzzy_symbols(msg)
        if guesses:
            symbols = [sym for _, sym in guesses]
            read_as = "(I read " + ", ".join(f"'{w}' as {LABELS.get(sym, sym)}" for w, sym in guesses) + ".)\n"
    if not symbols and last_symbol and _FOLLOWUP.search(msg) and len(msg.split()) <= 8 and re.fullmatch(r"[A-Z0-9.^=\-]{1,12}", last_symbol):
        symbols = [last_symbol]  # follow-up: "what about its risk?" after a ticker answer

    if _SMALLTALK.search(msg) and not symbols:
        return {"intent": "smalltalk", "text": _pick([
            f"Doing well, thanks{who}! Markets never sleep, so neither do I. Want a quick market check?",
            f"All good here{who} — the feeds are flowing. Name a ticker and I'll run the AI on it.",
            f"Great, thanks for asking{who}. Want the biggest movers today or the AI's read on something?"])}
    if _BYE.search(msg) and not symbols:
        return {"intent": "bye", "text": _pick([f"See you{who}! The charts will be here when you're back.",
                                                f"Bye{who} — take care, and remember: paper first, real money later."])}
    if _LAUGH.match(msg):
        return {"intent": "laugh", "text": _pick([f"😄 Anything you'd like me to look at{who}?",
                                                  "Glad that landed. Want a market check or the AI's read on a ticker?"])}
    if _JOKE.search(msg) and not symbols:
        return {"intent": "joke", "text": _pick(JOKES) + "\nNow, want a real market check?"}

    if _GREETING.match(msg) and len(msg.split()) <= 4 and not symbols:
        return {"intent": "greeting", "text": _pick([
            f"Hey{who}! I'm Loki. Name a ticker and I'll run the AI on it, ask how the market's doing, or ask me to explain any trading term.",
            f"Hi{who} — good to see you. Want a live market check, the AI's read on a stock or coin, or its track record?",
            f"Hello{who}! Ask me about any ticker (try 'what about NVDA?'), the market today, or how the AI works.",
        ])}
    if _THANKS.search(msg) and len(msg.split()) <= 6:
        return {"intent": "thanks", "text": _pick([f"Any time{who}!", f"Happy to help{who}. Anything else you want me to look at?",
                                                   f"You're welcome{who} — name another ticker whenever you like."])}
    if _IDENTITY.search(msg):
        return {"intent": "identity", "text": ("I'm Loki — the assistant for this trading workspace. I'm not a person and I'm not a general chatbot: "
                                               "I explain what the live market data and Loki's trading AI are saying, in plain English. " + HELP_TEXT.split('\n', 1)[1])}
    if _MAKER.search(msg):
        return {"intent": "identity", "text": maker_answer()}
    if _START.search(msg) and not symbols:
        return {"intent": "getting_started", "text": START_TEXT}
    if _HELP.search(msg) and not symbols:
        return {"intent": "help", "text": HELP_TEXT}
    if _ADVICE_Q.search(msg):
        return {"intent": "advice_disclaimer", "text": ADVICE_TEXT}
    if _PRIVACY.search(msg) and not symbols:
        return {"intent": "privacy", "text": PRIVACY_TEXT}
    if _FEELINGS.search(msg) and not symbols:
        return {"intent": "feelings", "text": feelings_answer(name)}
    if _TIPS.search(msg) and not symbols:
        return {"intent": "tips", "text": tips_answer(name)}
    if _BROKER.search(msg) and not symbols:
        return {"intent": "broker", "text": BROKER_TEXT}
    if _GAME.search(msg) and not symbols and len(msg.split()) <= 8:
        return {"intent": "game", "text": GAME_TEXT}
    if allow_data and _PORTFOLIO_Q.search(msg) and not symbols and not re.search(r"\bmy\b", low):
        return {"intent": "portfolio", "text": portfolio_answer()}
    if _EVOLVE.search(msg) and not symbols and re.search(r"\b(ai|loki|you|model|bot|strategy|it)\b", low):
        return {"intent": "evolution", "text": evolution_answer()}
    if _HOW_AI.search(msg):
        return {"intent": "how_ai", "text": how_ai_works()}
    if _TRUST.search(msg) and not symbols and re.search(r"\b(ai|loki|you|model|bot|strategy|it|this)\b", low):
        return {"intent": "trust", "text": trust_answer()}
    if _TRACK.search(msg) and not symbols and re.search(r"\b(ai|loki|you|model|bot|strategy|it)\b", low):
        return {"intent": "track_record", "text": track_record() + "\n" + FOOTER}
    if allow_data and _ODDS.search(msg) and re.search(r"\b(odds|chances?|probability|polymarket|prediction|bet|crowd|will the fed|rate (cut|hike)|recession|ipo)\b", low):
        text = odds_answer(msg)
        if text:
            return {"intent": "odds", "text": text}
    if symbols and re.match(r"^\s*(what('s| is| are)|explain|define)\b", msg, re.I) and not _ADVICE.search(msg):
        term = _glossary(msg)
        if term:
            return {"intent": "glossary", "text": term}
    if allow_data and symbols and len(symbols) == 1 and _PRICE_ONLY.search(msg) and not _ANALYSIS_WORDS.search(msg):
        quick = quote_answer(symbols[0])
        if quick:
            return {"intent": "price", "symbols": symbols, "text": read_as + quick}
    if allow_data and symbols and (_ADVICE.search(msg) or len(msg.split()) <= 4 or "?" in msg or read_as):
        pairs: List[Tuple[str, str]] = []
        for sym in symbols[:2]:
            reply = explain_symbol(sym, name)
            if reply:
                pairs.append((sym, reply))
        if not pairs:
            return None
        body = "\n\n".join(r for _, r in pairs)
        if len(pairs) == 2 and re.search(r"\b(vs\.?|versus|compare|or|better)\b", low):
            def verdict(reply: str) -> str:
                m = re.search(r"Verdict: ([A-Z ]+?)(?: —| setup|,)", reply)
                return m.group(1).strip().title() if m else "no read"
            body = ("Side by side: " + " · ".join(f"{s} — {verdict(r)}" for s, r in pairs) +
                    ". The AI judges each market on its own rules; it doesn't rank one against the other.\n\n" + body)
        if re.search(r"\bshould i\b", low):
            body = "I can't tell you what to do with your money, but here's exactly what the AI sees:\n" + body
        return {"intent": "symbol", "symbols": symbols[:2], "text": read_as + body}
    if allow_data and _MOVERS.search(msg) and not symbols:
        return {"intent": "movers", "text": movers_answer()}
    if allow_data and _TIMING.search(msg) and not symbols:
        return {"intent": "timing", "text": timing_answer()}
    if allow_data and _MARKET.search(msg):
        return {"intent": "market", "text": market_overview()}
    term = _glossary(msg)
    if term and (_ASK_AI_DEFINE.search(msg) or len(msg.split()) <= 5 or "?" in msg):
        return {"intent": "glossary", "text": term + "\n" + _pick(["Want me to show how the AI uses it on a real ticker?",
                                                                   "Name a ticker and I'll show it in action.",
                                                                   "Anything else you'd like explained?"])}
    if _OFFTOPIC.search(msg) and not symbols:
        return {"intent": "off_topic", "text": OFFTOPIC_TEXT}
    return None

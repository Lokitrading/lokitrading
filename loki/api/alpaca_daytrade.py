"""The Day Trader on the visitor's own Alpaca PAPER account.

Each call is one check: read the Day Trader's plan (the same tested rules as the
AI trading page), read the paper account, and send the orders that make the
account hold what the rules hold. The browser calls it every 30 seconds while
the visitor's tab is open, and again just after each half hour, when the rules
decide. Nothing is stored here: the keys arrive with each call and go only to
Alpaca's paper endpoint.

Guards, all checked on every call:

* Paper only. The host is pinned to ``paper-api.alpaca.markets``.
* Only the Day Trader's own markets are touched. At start, the account must not
  already hold any of them, so an existing position is never closed by mistake.
* At least US$2,000 of equity, the margin-account minimum shorts need. (FINRA's
  US$25,000 pattern-day-trader minimum ended on 4 June 2026; brokers phase in the new
  intraday-margin checks until October 2027, so a broker refusal for buying power or
  margin is reported and retried on the next check, never treated as bad keys.)
* New positions never use more than 95% of the buying power Alpaca reports.
* Everything is closed from 10 minutes before Alpaca's own closing time (which also
  covers early-close days), so an API hiccup at the bell cannot leave a position overnight.
* A daily loss stop: if the account is down 2% or more since yesterday's close,
  every Day Trader position is closed and no new one is opened that day. In the
  10-year test the book fell that far on one day out of about 2,700.
* Whole shares, at most US$50,000 and 5,000 shares per market. A short is opened
  only in a stock Alpaca lists as easy to borrow.
* A position is only opened or closed when the rules change direction. It is not
  resized during the day, so one check never fights another.
* A new position opens only within 5 minutes of the rules' signal (the delay the
  test measured); a missed signal waits for the next one. Closing never expires.
* No new position while the quoted spread is wider than 0.10% of the price.
* No new position before 09:45 New York time, while the opening auctions settle and
  spreads are wide. (The rules' first decision is at 10:00, so today this never bites;
  it stays as a line in the policy.) Closing is always allowed.
* No new position in a stock that may be halted: the quote must be two-sided (bid and
  ask above 0), uncrossed (ask above bid) and at most 2 minutes old, and Alpaca must
  list the asset as active and tradable.
* No new position on the ex-date of a split, reverse split, stock dividend, spin-off
  or special dividend (Alpaca's corporate-actions data), when yesterday's close and
  today's prices are not comparable. If that data cannot be read, no new position.
* Quotes for every market come from one snapshot call per feed, so all are from the
  same moment, and asset flags are read once a day.
* Alpaca's request budget (its X-RateLimit headers) is watched on every answer. Below
  15% left, the check skips the drift reading and opens nothing, keeping what is left
  for closing orders.
* Times come from Alpaca's clock, not this server's, so the 5-minute signal limit and
  the 2-minute quote limit hold even if the server's clock drifts.
* Every Alpaca answer is checked against a schema before it is used; a malformed
  answer trades nothing. Sizes are computed in exact decimals.
* Every order carries a deterministic client order id, so a retried request can
  never place the same order twice, and a receipt: a SHA-256 hash of the market
  snapshot (with the quote feed it came from), the risk policy, the drift state and
  the checks it passed. A blocked order gets a receipt too, saying which check stopped
  it. The page adds each receipt to the device's signed, hash-chained trade log.
* A drift alarm (loki_v8/drift.py) compares the account's own recent round trips and
  fills with the tested record and shrinks new positions to x0.5 or x0.1 when they drift.
* New positions open with marketable limit orders: immediate-or-cancel, at most 0.05%
  through the quote (rounded to the cent, inside the cap), so a sudden gap in liquidity
  cannot fill an entry far from the price the guards just checked. An unfilled order
  is cancelled by Alpaca at once and retried on later checks, at most 3 times per
  signal. Closing (a rule change, the loss stop, the bell, Stop) stays a market order,
  so getting out is never held up by a price cap. Whole shares only, regular hours.
* Every check is a fresh reconciliation: positions and today's orders are read from Alpaca
  before anything is decided, so a restarted server or a closed tab never orphans one.
  A reversal waits until Alpaca shows the old position closed, and a market with an
  order still working is left alone, so opposite orders never cross (a wash-trade refusal).

Results are not guaranteed. The rules made money on the years they never saw,
but they lost money on most of the markets tested, and late fills cost a lot.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from loki.api.order_safety import submit_once
from loki.api.broker_alpaca import (
    _DATA_BASE,
    RATE_BUDGET,
    AlpacaPaperKeys,
    _exchange,
    _number_or_none,
    _order_view,
    _paper_exchange,
    _require_gate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/broker/alpaca", tags=["Broker relay"])

# --- The risk policy (every limit in one place; its hash goes into each receipt) ---------------
POLICY: Dict[str, Any] = {
    "version": 3,
    "venue": "alpaca-paper",
    "min_equity_usd": 2_000,  # margin-account minimum (shorts need margin)
    "max_buying_power_used": "0.95",  # new positions never use more of Alpaca's reported buying power
    "flatten_minutes_before_close": 10,  # close everything this long before Alpaca's closing time
    "quote_max_age_seconds": 120,  # an older quote is not clean
    "drift": {"min_trades": 20, "window": 30, "amber": "z<-1.5 or payoff<60% or slippage>2x cost: size x0.5",
              "red": "z<-2.5 or payoff<35%: size x0.1"},
    "daily_loss_stop_pct": 2.0,  # close everything and stop for the day at this loss
    "max_per_market_usd": 50_000,  # cap on one market's position
    "max_shares": 5_000,
    "size_buffer": "0.95",  # leave room for the price moving between the check and the fill
    "signal_ttl_seconds": 300,  # a new position opens only this soon after the rules' signal
    "max_spread_pct": 0.10,  # no new position while the quoted spread is wider than this
    "shorts": "easy-to-borrow only",
    "opening_lockout_until": "09:45",  # New York time: no new position before this; closing is always allowed
    "halt_check": "two-sided, uncrossed, fresh quote and an active, tradable asset",
    "corporate_actions": "no new position on the ex-date of a split, reverse split, stock dividend, spin-off or special dividend",
    "rate_limit_reserve_pct": 15,  # below this share of Alpaca's request budget, open nothing
    "entry_order": "limit, immediate-or-cancel, regular hours",
    "limit_through_quote_pct": "0.05",  # how far past the ask (buy) or bid (sell) an entry may fill
    "max_entry_tries": 3,  # unfilled immediate-or-cancel entries per signal
    "exit_order": "market, day, regular hours",
    "clock": "alpaca",
}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
MIN_EQUITY = float(POLICY["min_equity_usd"])
DAILY_LOSS_STOP_PCT = float(POLICY["daily_loss_stop_pct"])
MAX_PER_MARKET = Decimal(POLICY["max_per_market_usd"])
MAX_SHARES = int(POLICY["max_shares"])
SIZE_BUFFER = Decimal(POLICY["size_buffer"])
SIGNAL_TTL_SECONDS = int(POLICY["signal_ttl_seconds"])
BUYING_POWER_USE = Decimal(POLICY["max_buying_power_used"])
FLATTEN_MINUTES = int(POLICY["flatten_minutes_before_close"])
QUOTE_MAX_AGE = int(POLICY["quote_max_age_seconds"])
DRIFT_LOOKBACK_DAYS = 45
MAX_SPREAD_PCT = float(POLICY["max_spread_pct"])
FLAT_WAIT_SECONDS = (1.0, 1.5, 2.0)  # after closing, how long to wait for flat before reversing
OPENING_LOCKOUT = dt_time.fromisoformat(POLICY["opening_lockout_until"])
RATE_RESERVE = POLICY["rate_limit_reserve_pct"] / 100
LIMIT_THROUGH = Decimal(POLICY["limit_through_quote_pct"]) / 100
MAX_TRIES = int(POLICY["max_entry_tries"])
NEW_YORK = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
# Order states in which an order is still working at Alpaca.
WORKING = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding", "pending_cancel",
           "pending_replace", "held", "calculated"}
# Corporate actions that break the comparison between yesterday's close and today's prices.
PRICE_BREAKS = {"forward_splits": "a stock split", "reverse_splits": "a reverse split", "unit_splits": "a unit split",
                "stock_dividends": "a stock dividend", "spin_offs": "a spin-off", "cash_dividends": "a special dividend"}
CACHE_SECONDS = 30 * 60  # asset flags and corporate actions are re-read at most every half hour
_CACHE: Dict[Tuple[str, str], Tuple[float, Any]] = {}


# --- What Alpaca must send before anything is traded --------------------------------------------
def _finite(value: Any) -> float:
    number = _number_or_none(value)
    if number is None:
        raise ValueError("not a finite number")
    return number


def _when(stamp: Any) -> Optional[datetime]:
    """An ISO time from Alpaca (a trailing Z and nanoseconds allowed) as an aware datetime, or None."""
    text = str(stamp or "").strip().replace("Z", "+00:00")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})?", text)
    if not match or not match.group(3):
        return None
    try:
        return datetime.fromisoformat(match.group(1) + (match.group(2) or "")[:7] + match.group(3))
    except ValueError:
        return None


class AlpacaAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")
    equity: float = Field(ge=0)
    last_equity: float = Field(ge=0)
    buying_power: float = Field(ge=0)
    trading_blocked: bool = False
    account_blocked: bool = False

    @field_validator("equity", "last_equity", "buying_power", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    is_open: bool
    timestamp: Optional[str] = None
    next_open: Optional[str] = None
    next_close: Optional[str] = None

    def minutes_to_close(self) -> Optional[float]:
        close, now = _when(self.next_close), _when(self.timestamp)
        return None if close is None or now is None else (close - now).total_seconds() / 60


class AlpacaPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str = Field(min_length=1, max_length=12)
    qty: float
    side: Literal["long", "short"]

    @field_validator("qty", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ap: float = Field(gt=0)  # ask
    bp: float = Field(gt=0)  # bid
    t: Optional[str] = None  # quote time

    @field_validator("ap", "bp", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


class AlpacaFill(BaseModel):
    """One fill from the account's activity feed (for the drift alarm)."""

    model_config = ConfigDict(extra="ignore")
    symbol: str
    side: str
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    transaction_time: str

    @field_validator("qty", "price", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> float:
        return _finite(value)


def _valid(model: Any, payload: Any, what: str) -> Any:
    """An Alpaca answer, checked against its schema; a malformed one stops the check before any trade."""
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        logger.warning(json.dumps({"event": "alpaca_malformed", "what": what, "errors": error.error_count()}))
        raise HTTPException(502, f"Alpaca sent a malformed {what}; nothing was traded on this check.") from error


class AlpacaDayTradeCheck(AlpacaPaperKeys):
    """One check of the Day Trader on this paper account."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal["day trade paper money"]
    start: bool = False  # the first check: refuse if the account already holds the Day Trader's markets
    stop: bool = False  # close every Day Trader position now and trade no more


def signed_shares(position: Optional[Dict[str, Any]]) -> int:
    """Shares held: positive long, negative short, 0 for none."""
    if not position:
        return 0
    try:
        qty = abs(float(position.get("qty") or 0))
    except (TypeError, ValueError):
        return 0
    qty = int(round(qty))
    return -qty if str(position.get("side") or "").lower() == "short" else qty


def signal_age(market: Dict[str, Any], now: str) -> Optional[float]:
    """Seconds since the rules decided the market's current stance (None when unknown)."""
    decided, at = _when(market.get("signal_at")), _when(now)
    return None if decided is None or at is None else (at - decided).total_seconds()


def order_base(symbol: str, signal_at: Any, shares: int) -> str:
    """What every entry try for one signal has in common: market, signal time and side."""
    stamp = "".join(ch for ch in str(signal_at or "unknown") if ch.isalnum())[:16]
    return f"loki-dt-{symbol}-{stamp}-{'buy' if shares > 0 else 'sell'}"


def client_order_id(symbol: str, signal_at: Any, shares: int, ref_price: float = 0.0, attempt: int = 1) -> str:
    """The same signal and try always give the same id, so Alpaca refuses a second copy of an order.

    Two checks running at once read the same orders, number their try alike, and so send the
    same id: Alpaca keeps one. The id also carries the price the rules decided on (the decision
    bar's close, in cents), so the drift alarm can measure each fill's slippage from Alpaca's records.
    """
    cents = int(round(ref_price * 100)) if ref_price and ref_price > 0 else 0
    return f"{order_base(symbol, signal_at, shares)}-a{attempt}-r{cents}"


def entry_tries(orders: List[Dict[str, Any]], base: str) -> Tuple[int, bool]:
    """(tries already sent for this signal, whether any of them filled at all)."""
    tries = [o for o in orders if str(o.get("client_order_id") or "").startswith(base + "-a")]
    filled = any((_number_or_none(o.get("filled_qty")) or 0) > 0 for o in tries)
    return len(tries), filled


def limit_price(shares: int, bid: float, ask: float) -> Decimal:
    """A marketable limit: at most LIMIT_THROUGH past the ask (buy) or bid (sell), in whole cents, inside the cap."""
    if shares > 0:
        ref = Decimal(str(ask))
        return max((ref * (1 + LIMIT_THROUGH)).quantize(CENT, rounding=ROUND_FLOOR), ref.quantize(CENT, rounding=ROUND_CEILING))
    ref = Decimal(str(bid))
    return min((ref * (1 - LIMIT_THROUGH)).quantize(CENT, rounding=ROUND_CEILING), ref.quantize(CENT, rounding=ROUND_FLOOR))


def opening_lockout(now: Optional[datetime]) -> bool:
    """True before 09:45 New York time (and when the time is unknown): no new position yet."""
    return now is None or now.astimezone(NEW_YORK).time() < OPENING_LOCKOUT


def budget_low(budget: Optional[Dict[str, Any]]) -> bool:
    """True when Alpaca reports less than the reserve share of its request budget left."""
    return bool(budget) and int(budget["remaining"]) < int(budget["limit"]) * RATE_RESERVE  # type: ignore[index]


def clean_quote(raw: Any, feed: str, now: Optional[datetime]) -> Tuple[Optional[Dict[str, Any]], str]:
    """A quote fit to trade on, or why not. A halted stock shows a one-sided, crossed or stale quote."""
    try:
        quote = AlpacaQuote.model_validate(raw)
    except ValidationError:
        return None, f"no two-sided quote on {feed} (bid or ask missing or zero; the stock may be halted)"
    if quote.ap <= quote.bp:
        return None, f"crossed or locked quote on {feed} (bid {quote.bp} >= ask {quote.ap}; the stock may be halted)"
    stamp = _when(quote.t)
    if stamp is None or now is None:
        return None, f"the quote on {feed} has no usable time"
    age = (now - stamp).total_seconds()
    if age > QUOTE_MAX_AGE:
        return None, f"the latest quote on {feed} is {int(age)} s old (limit {QUOTE_MAX_AGE} s; the stock may be halted)"
    mid = (quote.ap + quote.bp) / 2
    return {"bid": quote.bp, "ask": quote.ap, "spread_pct": round((quote.ap - quote.bp) / mid * 100, 4),
            "feed": feed, "quote_age_s": round(age, 1)}, ""


def corporate_action_breaks(raw: Any, symbols: List[str], day: str) -> Optional[Dict[str, str]]:
    """Markets with a price-breaking corporate action going ex on ``day``; None if the answer is unreadable."""
    actions = raw.get("corporate_actions") if isinstance(raw, dict) else None
    if not isinstance(actions, dict):
        return None
    out: Dict[str, str] = {}
    for kind, items in actions.items():
        label = PRICE_BREAKS.get(kind)
        for item in items if label and isinstance(items, list) else []:
            if not isinstance(item, dict) or (kind == "cash_dividends" and not item.get("special")):
                continue
            if str(item.get("ex_date") or item.get("effective_date") or "") != day:
                continue
            for field in ("symbol", "source_symbol", "old_symbol"):
                if item.get(field) in symbols:
                    out[str(item[field])] = f"{label} goes ex today ({day}), so yesterday's close is not comparable"
    return out


def slippage_from_orders(orders: List[Dict[str, Any]]) -> List[float]:
    """Basis points each Day Trader entry filled against itself, from its id's decision price."""
    out = []
    for order in orders:
        cid = str(order.get("client_order_id") or "")
        if not cid.startswith("loki-dt-") or "-r" not in cid:
            continue
        try:
            ref = int(cid.rsplit("-r", 1)[1]) / 100
            fill = float(order.get("filled_avg_price") or 0)
        except ValueError:
            continue
        if ref <= 0 or fill <= 0:
            continue
        sign = 1 if "-buy-" in cid else -1
        out.append(round(sign * (fill - ref) / ref * 1e4, 2))
    return out


def receipt(trace_id: str, symbol: str, action: str, shares: int, snapshot: Dict[str, Any], checks: List[str]) -> Dict[str, Any]:
    """What was known and checked when an order was sent, sealed with a SHA-256 hash."""
    body = {"trace_id": trace_id, "symbol": symbol, "action": action, "shares": shares, "policy": POLICY_HASH,
            "snapshot": snapshot, "checks_passed": checks, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {**body, "hash": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def wanted_side(market: Dict[str, Any]) -> int:
    """What the rules want this market to hold now: 1 long, -1 short, 0 flat."""
    if market.get("status") != "open" or market.get("waiting") or market.get("skipped"):
        return 0
    pending = market.get("pending")
    side = pending if pending is not None else market.get("position")
    try:
        side = int(side or 0)
    except (TypeError, ValueError):
        return 0
    return max(-1, min(1, side))


def target_shares(side: int, equity: float, weight: float, price: float) -> int:
    """Whole shares for one market's equal share of the account, within the caps."""
    try:
        equity_d, weight_d, price_d = Decimal(str(equity)), Decimal(str(weight)), Decimal(str(price))
    except InvalidOperation:
        return 0
    if side == 0 or not price_d.is_finite() or not equity_d.is_finite() or price_d <= 0 or equity_d <= 0 or weight_d <= 0:
        return 0
    share = (equity_d * weight_d).quantize(Decimal("0.01"))  # to the cent, so 1/6 of $120,000 is $20,000.00
    budget = min(share, MAX_PER_MARKET) * SIZE_BUFFER
    return side * min(int((budget / price_d).to_integral_value(rounding=ROUND_FLOOR)), MAX_SHARES)


def plan_orders(markets: List[Dict[str, Any]], held: Dict[str, int], equity: float, weight: float,
                busy: set, shortable: Dict[str, bool], stop: bool, now: str = "") -> List[Dict[str, Any]]:
    """For each market, what to do now. Pure: no network, so it can be tested on its own."""
    rows = []
    for market in markets:
        symbol = str(market["symbol"])
        price = _number_or_none(market.get("now_price")) or _number_or_none(market.get("last")) or 0.0
        side = 0 if stop else wanted_side(market)
        note = ""
        if side < 0 and not shortable.get(symbol, False):
            side, note = 0, "the rules are short, but Alpaca does not list this stock as easy to borrow, so it stays flat"
        target = target_shares(side, equity, weight, price)
        if side and target == 0:
            note = "one share costs more than this market's share of the account"
        holding = held.get(symbol, 0)
        if symbol in busy:
            action, note = "wait", "an order for this market is still being filled"
        elif holding == 0 and target == 0:
            action = "none"
        elif holding == 0:
            action = "open"
        elif target == 0 or (holding > 0) != (target > 0):
            action = "close_then_open" if target else "close"
        else:
            action = "hold"  # same direction: positions are not resized during the day
        age = signal_age(market, now) if now else None
        if action in ("open", "close_then_open") and age is not None and age > SIGNAL_TTL_SECONDS:
            action = "close" if action == "close_then_open" else "none"
            note = f"missed this signal ({int(age // 60)} min old); it waits for the rules' next decision"
        rows.append({"symbol": symbol, "rules": side, "price": round(price, 4), "held": holding, "target": target,
                     "action": action, "note": note, "signal_at": market.get("signal_at"),
                     "ref_price": _number_or_none(market.get("last")) or price,
                     "cost_bps": _number_or_none(market.get("cost_bps")) or 2.0})
    return rows


def entry_blocker(row: Dict[str, Any], now: Optional[datetime], budget: Optional[Dict[str, Any]],
                  breaks: Optional[Dict[str, str]], assets: Dict[str, Dict[str, bool]],
                  orders: List[Dict[str, Any]]) -> Optional[str]:
    """Why a new position may not open now (None when every entry guard before the quote passes). Pure."""
    symbol = row["symbol"]
    if opening_lockout(now):
        return f"opening lockout: no new position before {POLICY['opening_lockout_until']} New York time"
    if budget and budget_low(budget):
        return f"Alpaca's request budget is low ({budget['remaining']} of {budget['limit']} left), so it is kept for closing orders"
    if breaks is None:
        return "Alpaca's corporate-actions data could not be read, so a split or special dividend cannot be ruled out"
    if symbol in breaks:
        return breaks[symbol]
    flags = assets.get(symbol) or {}
    if not (flags.get("active") and flags.get("tradable")):
        return "Alpaca does not list this stock as active and tradable right now"
    tries, filled = entry_tries(orders, order_base(symbol, row["signal_at"], row["target"]))
    if filled:
        return "an entry on this signal already filled; it waits for the rules' next decision"
    if tries >= MAX_TRIES:
        return f"no fill within {POLICY['limit_through_quote_pct']}% of the quote after {MAX_TRIES} tries; it waits for the next signal"
    return None


def size_entry(row: Dict[str, Any], quote: Dict[str, Any], room: Decimal) -> Tuple[int, Decimal, Optional[str]]:
    """(shares, limit price, why not) for a new position: the capped limit, cut to the buying power left. Pure."""
    shares = int(row["target"])
    limit = limit_price(shares, quote["bid"], quote["ask"])
    if limit < 1:
        return 0, limit, "the price is under US$1, where Alpaca's tick rules differ"
    worst = max(limit, Decimal(str(row["price"])))  # buying power is checked at the worse price
    fits = int((room / worst).to_integral_value(rounding=ROUND_FLOOR))
    if abs(shares) > fits:
        shares = fits if shares > 0 else -fits
    if shares == 0:
        return 0, limit, "not enough buying power left for this market"
    return shares, limit, None


def entry_checks(quote: Dict[str, Any], limit: Decimal, tries: int) -> List[str]:
    """The guards a sent entry passed, for its receipt."""
    return [f"signal under {SIGNAL_TTL_SECONDS // 60} min old", f"spread <= {MAX_SPREAD_PCT}% ({quote['feed']})",
            "two-sided, uncrossed, fresh quote", "asset active and tradable", "no price-breaking corporate action today",
            f"after {POLICY['opening_lockout_until']} New York", "request budget above 15%",
            "size within US$50,000 and 5,000 shares", "within 95% of buying power",
            f"limit {limit} ({POLICY['limit_through_quote_pct']}% cap), try {tries + 1} of {MAX_TRIES}"]


def _account(keys: AlpacaPaperKeys, stopping: bool) -> Tuple[AlpacaAccount, float, float]:
    """The paper account, its equity and today's change (%), refusing a blocked or too-small account."""
    account = _valid(AlpacaAccount, _paper_exchange("GET", "/v2/account", keys), "account")
    if account.trading_blocked or account.account_blocked:
        raise HTTPException(422, "Alpaca has blocked trading on this paper account.")
    equity = account.equity
    last_equity = account.last_equity or equity
    if not stopping and equity < MIN_EQUITY:
        raise HTTPException(422, f"Day trading needs at least US${MIN_EQUITY:,.0f} in the account (the margin-account minimum "
                                 f"that short sales need). This paper account has US${equity:,.2f}. "
                                 "You can reset a paper account to a larger balance in the Alpaca dashboard.")
    return account, equity, (equity / last_equity - 1.0) * 100 if last_equity > 0 else 0.0


def _held(keys: AlpacaPaperKeys, symbols: List[str]) -> Dict[str, int]:
    raw = _paper_exchange("GET", "/v2/positions", keys)
    if not isinstance(raw, list):
        raise HTTPException(502, "Alpaca's positions answer was not the expected list.")
    positions = [_valid(AlpacaPosition, p, "position") for p in raw]
    return {p.symbol: signed_shares(p.model_dump()) for p in positions if p.symbol in symbols}


def _quotes(keys: AlpacaPaperKeys, symbols: List[str], now: Optional[datetime]) -> Dict[str, Tuple[Optional[Dict[str, Any]], str]]:
    """The latest clean quote for each market (or why there is none), one snapshot call per feed.

    The consolidated SIP feed is tried first (it needs a paid Alpaca data plan); markets without a
    clean SIP quote are tried on the free IEX feed, which carries a small share of volume and can
    show a wider spread than the whole market. One call covers every market, so all the quotes are
    from the same moment. The feed is recorded in every receipt so missed entries can be traced to it.
    """
    out: Dict[str, Tuple[Optional[Dict[str, Any]], str]] = {s: (None, "no quote on SIP or IEX") for s in symbols}
    answered: set = set()  # markets a feed actually answered for: its reason is kept over a later feed's
    for feed in ("sip", "iex"):
        wanted = [s for s in symbols if out[s][0] is None]
        if not wanted:
            break
        try:
            raw = _exchange("GET", f"/v2/stocks/snapshots?symbols={urlquote(','.join(wanted), safe=',')}&feed={feed}",
                            keys, None, _DATA_BASE)
        except HTTPException as error:
            if error.status_code == 401:
                raise
            continue  # e.g. a free data plan refusing SIP
        snapshots = raw.get("snapshots", raw) if isinstance(raw, dict) else {}
        for symbol in wanted:
            snap = snapshots.get(symbol) if isinstance(snapshots, dict) else None
            quote, why = clean_quote(snap.get("latestQuote") if isinstance(snap, dict) else None, feed.upper(), now)
            if quote is not None or symbol not in answered:
                out[symbol] = (quote, why)
            answered.add(symbol)
    return out


def _cached(name: str, day: str, read: Any) -> Any:
    """Market-wide facts (not tied to an account) read at most once per half hour per server instance."""
    key = (name, day)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = read()
    if value is not None:
        for old in [k for k in _CACHE if k[1] != day]:
            _CACHE.pop(old, None)
        _CACHE[key] = (time.monotonic(), value)
    return value


def _assets(keys: AlpacaPaperKeys, symbols: List[str], day: str) -> Dict[str, Dict[str, bool]]:
    """Alpaca's flags for each market: active, tradable, shortable, easy to borrow."""
    def read(symbol: str) -> Dict[str, bool]:
        raw = _paper_exchange("GET", f"/v2/assets/{symbol}", keys)
        raw = raw if isinstance(raw, dict) else {}
        return {"active": str(raw.get("status") or "").lower() == "active", "tradable": raw.get("tradable") is True,
                "shortable": raw.get("shortable") is True, "easy_to_borrow": raw.get("easy_to_borrow") is True}
    return {s: _cached(f"asset:{s}", day, lambda s=s: read(s)) for s in symbols}


def _corporate_actions(keys: AlpacaPaperKeys, symbols: List[str], day: str) -> Optional[Dict[str, str]]:
    """Markets with a price-breaking corporate action going ex today; None when Alpaca's data cannot be read."""
    def read() -> Optional[Dict[str, str]]:
        start = (datetime.fromisoformat(day) - timedelta(days=5)).date().isoformat()
        end = (datetime.fromisoformat(day) + timedelta(days=5)).date().isoformat()
        try:
            raw = _exchange("GET", f"/v1/corporate-actions?symbols={','.join(sorted(symbols))}&types="
                                   + ",".join(k[:-1] for k in PRICE_BREAKS) + f"&start={start}&end={end}&limit=1000",
                            keys, None, _DATA_BASE)
        except HTTPException as error:
            if error.status_code == 401:
                raise
            return None
        return corporate_action_breaks(raw, symbols, day)
    found: Optional[Dict[str, str]] = _cached("corporate-actions:" + ",".join(sorted(symbols)), day, read)
    return found


def _drift(keys: AlpacaPaperKeys, symbols: List[str], markets: List[Dict[str, Any]],
           baselines: Dict[str, Dict[str, Any]], today: str) -> Dict[str, Any]:
    """The drift alarm from the account's own fills and orders over the last weeks."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from loki_v8 import drift

    after = (datetime.fromisoformat(today) - timedelta(days=DRIFT_LOOKBACK_DAYS)).date().isoformat()
    try:
        raw_fills = _paper_exchange("GET", f"/v2/account/activities/FILL?after={after}&direction=asc&page_size=100", keys)
        raw_orders = _paper_exchange("GET", f"/v2/orders?status=closed&after={after}T00:00:00Z&limit=500&direction=asc", keys)
    except HTTPException as error:
        if error.status_code == 401:
            raise
        return {"tier": "green", "multiplier": 1.0, "trades": 0, "reasons": ["drift data unavailable this check; size stays x1.0"]}
    ny = ZoneInfo("America/New_York")
    fills = []
    for item in raw_fills if isinstance(raw_fills, list) else []:
        try:
            fill = AlpacaFill.model_validate(item)
        except ValidationError:
            continue  # one malformed fill is left out, never guessed
        day = datetime.fromisoformat(fill.transaction_time.replace("Z", "+00:00")).astimezone(ny).date().isoformat()
        fills.append({"symbol": fill.symbol, "side": fill.side, "qty": fill.qty, "price": fill.price, "day": day})
    trips = [t for t in drift.round_trips(fills, symbols) if t["day"] < today]  # finished days only
    slippage = slippage_from_orders([o for o in raw_orders if isinstance(o, dict)] if isinstance(raw_orders, list) else [])
    costs = [_number_or_none(m.get("cost_bps")) or 2.0 for m in markets]
    return drift.assess(trips, baselines, slippage, sum(costs) / len(costs) if costs else 2.0)


def _order(keys: AlpacaPaperKeys, symbol: str, shares: int, order_id: str, limit: Decimal) -> Dict[str, Any]:
    """An entry: a marketable limit order that fills at once or is cancelled (immediate-or-cancel)."""
    body = {"symbol": symbol, "qty": str(abs(shares)), "side": "buy" if shares > 0 else "sell",
            "type": "limit", "limit_price": str(limit), "time_in_force": "ioc", "extended_hours": False,
            "client_order_id": order_id}
    return submit_once(_paper_exchange, keys, body)  # a lost answer is looked up by id before it is reported


def _check(body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    token = RATE_BUDGET.set({})  # Alpaca's X-RateLimit headers land here during this check
    try:
        return _run_check(body)
    finally:
        RATE_BUDGET.reset(token)


def _run_check(body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    from loki.api import ai_paper

    trace_id = uuid.uuid4().hex[:16]
    keys = AlpacaPaperKeys(key_id=body.key_id, secret_key=body.secret_key)
    budget = RATE_BUDGET.get()
    account, equity, day_change_pct = _account(keys, body.stop)
    clock = _valid(AlpacaClock, _paper_exchange("GET", "/v2/clock", keys), "clock")
    market_open = clock.is_open
    to_close = clock.minutes_to_close() if market_open else None
    closing_time = to_close is not None and to_close <= FLATTEN_MINUTES

    plan = ai_paper.ai_daytrade(Response(), mode="stocks")
    # Every time below is Alpaca's, not this server's: the clock answer is the reference.
    alpaca_now = _when(clock.timestamp)
    now = alpaca_now or _when(plan.get("served_at"))
    clock_skew_s = None if alpaca_now is None else round((datetime.now(timezone.utc) - alpaca_now).total_seconds(), 1)
    today = (now.astimezone(NEW_YORK).date().isoformat() if now else str(plan.get("served_at") or "")[:10])
    markets = list(plan.get("markets") or [])
    symbols = [str(m["symbol"]) for m in markets]
    held = _held(keys, symbols)
    if body.start and any(held.values()):
        listed = ", ".join(f"{s} ({q:+d} shares)" for s, q in held.items() if q)
        raise HTTPException(409, f"This paper account already holds {listed}. The Day Trader trades those markets itself and "
                                 "would close them. Close them first, or use a separate paper account (Alpaca lets you "
                                 "open several).")
    stopped_for_loss = day_change_pct <= -DAILY_LOSS_STOP_PCT
    stop = body.stop or stopped_for_loss or closing_time
    record = ai_paper._daytrade_record()
    if budget_low(budget):
        drift_state: Dict[str, Any] = {"tier": "green", "multiplier": 1.0, "trades": 0,
                                       "reasons": ["drift reading skipped: Alpaca's request budget is low"]}
    else:
        drift_state = _drift(keys, symbols, markets, record.get("market_baselines") or {}, today)
    multiplier = Decimal(str(drift_state["multiplier"]))

    # Today's orders (New York day): what is still working, and how many tries each signal has had.
    since = datetime.fromisoformat(today).replace(tzinfo=NEW_YORK).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_orders = _paper_exchange("GET", f"/v2/orders?status=all&after={since}&limit=500&direction=desc", keys)
    orders = [o for o in raw_orders if isinstance(o, dict)] if isinstance(raw_orders, list) else []
    busy = {str(o.get("symbol")) for o in orders if str(o.get("status") or "").lower() in WORKING}
    wanted = [str(m["symbol"]) for m in markets if wanted_side(m) != 0] if not stop else []
    assets = _assets(keys, wanted, today) if wanted else {}
    shortable = {s: a["shortable"] and a["easy_to_borrow"] for s, a in assets.items()}
    rows = plan_orders(markets, held, equity, float(plan.get("weight_per_market") or 0), busy, shortable, stop,
                       now=now.isoformat() if now else "")
    for row in rows:  # the drift alarm only ever shrinks new positions
        if row["target"] and multiplier < 1:
            shrunk = int((Decimal(abs(row["target"])) * multiplier).to_integral_value(rounding=ROUND_FLOOR))
            row["target"] = shrunk if row["target"] > 0 else -shrunk
            if row["target"] == 0 and row["action"] in ("open", "close_then_open"):
                row["action"] = "close" if row["action"] == "close_then_open" else "none"
    if closing_time:
        for row in rows:
            if row["held"]:
                row["note"] = f"closing: {FLATTEN_MINUTES} minutes or less to Alpaca's close"
    blocked = [{"symbol": r["symbol"], "reason": r["note"], "signal_at": r["signal_at"]}
               for r in rows if r["note"] and r["rules"] != 0 and r["target"] == 0 and not closing_time]
    entering = [r["symbol"] for r in rows if r["action"] in ("open", "close_then_open")] if market_open else []
    breaks = _corporate_actions(keys, symbols, today) if entering else {}
    quotes = _quotes(keys, entering, now) if entering else {}
    room = Decimal(str(account.buying_power)) * BUYING_POWER_USE  # buying power left for new positions
    logger.info(json.dumps({"event": "daytrade_check", "trace_id": trace_id, "market_open": market_open,
                            "plan": {r["symbol"]: r["action"] for r in rows}, "day_change_pct": round(day_change_pct, 3),
                            "rate_budget": budget or None, "clock_skew_s": clock_skew_s}))
    base_checks = ["paper venue", f"equity >= {MIN_EQUITY:,.0f}", f"day change > -{DAILY_LOSS_STOP_PCT}%",
                   "Day Trader market only", "no order already working", "schema-valid account, clock and positions",
                   f"drift {drift_state['tier']} (size x{drift_state['multiplier']})", "times from Alpaca's clock"]
    drift_note = {k: drift_state.get(k) for k in ("tier", "multiplier", "trades", "z", "payoff_vs_test", "slippage_vs_assumed")}

    def block(symbol: str, shares: int, why: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sealed = receipt(trace_id, symbol, "blocked", shares, {**(quote or {}), "equity": round(equity, 2),
                                                                 "drift": drift_note, "why": why}, base_checks)
        logger.info(json.dumps({"event": "order_blocked", "trace_id": trace_id, "symbol": symbol, "why": why,
                                "receipt": sealed["hash"]}))
        return {"symbol": symbol, "did": "open", "shares": shares, "status": "held back",
                "detail": f"{why}; it tries again on the next check", "receipt": sealed}

    actions: List[Dict[str, Any]] = []
    if market_open or body.stop:
        for row in rows:
            symbol, action = row["symbol"], row["action"]
            try:
                if action in ("close", "close_then_open"):
                    closed = _paper_exchange("DELETE", f"/v2/positions/{symbol}", keys)
                    actions.append({"symbol": symbol, "did": "close", "shares": -row["held"], "status": "sent",
                                    "order": _order_view(closed) if isinstance(closed, dict) else {},
                                    "receipt": receipt(trace_id, symbol, "close", -row["held"],
                                                       {"price": row["price"], "equity": round(equity, 2), "drift": drift_note,
                                                        "why": row["note"] or "the rules changed direction"}, base_checks)})
                    if action == "close_then_open":
                        for pause in FLAT_WAIT_SECONDS:  # Alpaca will not reverse a position while the close is pending
                            time.sleep(pause)
                            if _held(keys, [symbol]).get(symbol, 0) == 0:
                                break
                        else:
                            actions.append({"symbol": symbol, "did": "open", "shares": row["target"], "status": "next check",
                                            "detail": "the close had not filled yet; the new position opens on the next check"})
                            continue
                        action = "open"
                if action == "open":
                    why = entry_blocker(row, now, budget, breaks, assets, orders)
                    if why:
                        actions.append(block(symbol, row["target"], why))
                        continue
                    quote, no_quote = quotes.get(symbol) or (None, "no quote on SIP or IEX")
                    if quote is None or quote["spread_pct"] > MAX_SPREAD_PCT:
                        why = (f"no clean quote ({no_quote})" if quote is None
                               else f"spread {quote['spread_pct']:.3f}% on {quote['feed']} is wider than {MAX_SPREAD_PCT}%")
                        actions.append(block(symbol, row["target"], why, quote))
                        continue
                    shares, limit, why = size_entry(row, quote, room)
                    if why:
                        actions.append(block(symbol, row["target"], why, quote))
                        continue
                    room -= Decimal(abs(shares)) * max(limit, Decimal(str(row["price"])))
                    tries, _ = entry_tries(orders, order_base(symbol, row["signal_at"], shares))
                    checks = base_checks + entry_checks(quote, limit, tries)
                    order_id = client_order_id(symbol, row["signal_at"], shares, row["ref_price"], attempt=tries + 1)
                    sealed = receipt(trace_id, symbol, "open", shares,
                                     {**quote, "price": row["price"], "decision_price": row["ref_price"], "limit": str(limit),
                                      "equity": round(equity, 2), "drift": drift_note}, checks)
                    actions.append({"symbol": symbol, "did": "open", "shares": shares, "status": "sent",
                                    "order": _order(keys, symbol, shares, order_id, limit), "receipt": sealed})
                    logger.info(json.dumps({"event": "order_sent", "trace_id": trace_id, "symbol": symbol,
                                            "shares": shares, "client_order_id": order_id, "limit": str(limit),
                                            "receipt": sealed["hash"]}))
            except HTTPException as error:
                if error.status_code == 401:
                    raise
                # A broker refusal (buying power, intraday margin, a halted stock) is reported and tried again on the
                # next check; it never stops the loop and never counts as bad keys.
                actions.append({"symbol": symbol, "did": action, "shares": row["target"], "status": "refused",
                                "detail": str(error.detail)})
                logger.warning(json.dumps({"event": "order_refused", "trace_id": trace_id, "symbol": symbol,
                                           "detail": str(error.detail)[:200]}))

    return {
        "ok": True, "paper": True, "checked_at": now.isoformat() if now else plan.get("served_at"),
        "plan_built_at": plan.get("generated_at"), "trace_id": trace_id, "policy": POLICY, "policy_hash": POLICY_HASH,
        "market_open": market_open, "next_open": clock.next_open, "next_close": clock.next_close,
        "equity": round(equity, 2), "day_change_pct": round(day_change_pct, 3),
        "loss_stop_pct": DAILY_LOSS_STOP_PCT, "stopped_for_loss": stopped_for_loss, "stopped": body.stop,
        "markets": rows, "actions": actions, "drift": drift_state, "blocked": blocked,
        "closing_time": closing_time, "minutes_to_close": None if to_close is None else round(to_close, 1),
        "buying_power": round(account.buying_power, 2), "rate_budget": budget or None, "clock_skew_s": clock_skew_s,
        "record": {"unseen_instant": ((plan.get("record") or {}).get("book") or {}).get("test"),
                   "fill_speed": (plan.get("record") or {}).get("fill_speed")},
    }


@router.post("/daytrade", include_in_schema=False)
async def alpaca_daytrade(request: Request, body: AlpacaDayTradeCheck) -> Dict[str, Any]:
    """One check of the Day Trader on this PAPER account: read the plan, send what is needed."""
    _require_gate(request)
    result: Dict[str, Any] = await run_in_threadpool(_check, body)
    return result

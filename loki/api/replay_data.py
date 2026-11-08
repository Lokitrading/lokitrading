"""Historical stock/index bars for the on-device replay sandbox.

Crypto history comes straight from Binance's public market-data API in the
visitor's browser; stocks need this small relay because Yahoo's chart API
sends no CORS headers. Past windows never change, so finished windows are
marked cacheable at Vercel's edge and repeat replays cost no function call.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Response

from loki.api.market_feed import YAHOO_CHART_URL, MarketFeedError, _http_json

router = APIRouter(prefix="/api/replay", tags=["Replay sandbox"])

MAX_DAYS = {"1d": 3 * 366, "1h": 60}
INTRADAY_HISTORY_DAYS = 729  # Yahoo keeps about two years of hourly bars


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a date like 2020-03-01") from None


def parse_chart(payload: Any) -> List[Dict[str, float]]:
    """Clean OHLCV rows (t in ms) from a Yahoo chart answer; junk rows are dropped."""
    try:
        result = payload["chart"]["result"][0]
        stamps = result.get("timestamp") or []
        block = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        raise MarketFeedError("the chart answer had no bars") from None
    cols = [block.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    rows: List[Dict[str, float]] = []
    for i, stamp in enumerate(stamps):
        try:
            o, h, lo, c, v = (float(col[i]) if col[i] is not None else math.nan for col in cols)
        except (IndexError, TypeError, ValueError):
            continue
        if stamp is None or not all(math.isfinite(x) for x in (o, h, lo, c)) or min(o, h, lo, c) <= 0:
            continue
        if not lo <= min(o, c) <= max(o, c) <= h:
            continue
        rows.append({"t": int(stamp) * 1000, "o": o, "h": h, "l": lo, "c": c, "v": v if math.isfinite(v) and v >= 0 else 0.0})
    return rows


@router.get("/bars")
def replay_bars(
    response: Response,
    symbol: str = Query(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.^=\-]+$"),
    start: str = Query(min_length=10, max_length=10),
    end: str = Query(min_length=10, max_length=10),
    interval: str = Query(default="1d", pattern=r"^(1d|1h)$"),
) -> Dict[str, Any]:
    first, last = _parse_day(start, "start"), _parse_day(end, "end")
    if last <= first:
        raise HTTPException(422, "end must be after start")
    if (last - first).days > MAX_DAYS[interval]:
        raise HTTPException(422, f"{interval} replays cover at most {MAX_DAYS[interval]} days")
    today = datetime.now(timezone.utc).date()
    if interval == "1h" and (today - first).days > INTRADAY_HISTORY_DAYS:
        raise HTTPException(422, "hourly stock history only reaches back about two years; use daily bars")
    p1 = int(datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(last.year, last.month, last.day, tzinfo=timezone.utc).timestamp()) + 86400
    url = f"{YAHOO_CHART_URL}/{quote(symbol.upper(), safe='')}?" + urlencode(
        {"period1": p1, "period2": p2, "interval": interval, "includePrePost": "false"})
    try:
        bars = parse_chart(_http_json(url, timeout=10.0))
    except MarketFeedError as exc:
        raise HTTPException(503, f"History for {symbol.upper()} is unavailable right now ({exc}).") from None
    if len(bars) < 5:
        raise HTTPException(404, f"No usable {interval} bars for {symbol.upper()} in that window.")
    finished = p2 < time.time() - 2 * 86400
    response.headers["Cache-Control"] = ("public, max-age=3600, s-maxage=604800, immutable" if finished
                                         else "public, max-age=60, s-maxage=300")
    return {"symbol": symbol.upper(), "interval": interval, "source": "Yahoo Finance public chart", "bars": bars}

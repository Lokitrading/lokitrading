"""Small server relays the on-device engine cannot do from the browser.

* ``/api/newswire``: Fed, ECB and Bank of England press feeds (they send no
  CORS headers), with a transparent hawkish/dovish word score on monetary
  policy items. It is a keyword read, not an economist: the matched words are
  returned with every item so anyone can check the call.
* ``/api/xarb``: the same yes/no question listed on Kalshi and Polymarket.
  Titles are matched by word overlap and every pair is shown with the cost of
  buying YES on one and NO on the other. Read-only: Loki places no orders on
  either venue, and two questions that look alike can resolve differently, so
  every pair carries both rule texts and a warning.
* ``/api/status/telemetry``: this server instance's uptime, memory and CPU.
* ``/api/signals/relay``: forwards a short trade signal to a Telegram bot,
  Discord webhook or Slack webhook the visitor supplies. Fixed hosts only,
  nothing stored.
"""

from __future__ import annotations

import math
import os
import platform
import re
import resource
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

router = APIRouter(tags=["Engine relays"])

_STARTED = time.time()
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Any]] = {}
_UA = {"User-Agent": "LokiWorkspace/2.0 (public press feeds; read-only)", "Accept": "*/*"}
MAX_FEED_BYTES = 2_000_000


def _cached(key: str, ttl: float, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            cached: Dict[str, Any] = hit[1]
            return cached
    value = fn()
    with _lock:
        _cache[key] = (now, value)
    return value


def _fetch(url: str, timeout: float = 8.0) -> bytes:
    with requests.get(url, headers=_UA, timeout=(4, timeout), allow_redirects=False, stream=True) as res:
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}")
        chunks, size = [], 0
        for chunk in res.iter_content(65536):
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise RuntimeError("feed larger than expected")
            chunks.append(chunk)
        return b"".join(chunks)


# --------------------------------------------------------------- newswire
FEEDS = {
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
    "BoE": "https://www.bankofengland.co.uk/rss/news",
}
POLICY = re.compile(r"\b(rate|rates|monetary|fomc|policy statement|interest|inflation|bank rate|minutes|balance sheet)\b", re.I)
HAWKISH = (r"\brais(e|ed|es|ing)\b", r"\bincreas(e|ed|es)\b.*\brate", r"\brate\b.*\bincreas", r"\bhik(e|ed|es)\b", r"\btighten",
           r"\brestrictive\b", r"\bpersistent inflation\b", r"\binflation (remains|is) (elevated|high)", r"\bupside risks? to inflation")
DOVISH = (r"\bcut(s|ting)?\b", r"\blower(ed|s|ing)?\b.*\brate", r"\brate\b.*\b(reduc|lower|decreas)", r"\breduc(e|ed|es)\b.*\brate",
          r"\beas(e|ed|ing)\b", r"\baccommodat", r"\bstimulus\b", r"\bdownside risks?\b", r"\bdecreas(e|ed)\b.*\brate")
_HAWK = [re.compile(p, re.I) for p in HAWKISH]
_DOVE = [re.compile(p, re.I) for p in DOVISH]
HALF_LIFE_DAYS = 10.0


def score_text(text: str) -> Tuple[float, List[str]]:
    """-1 dovish .. +1 hawkish for one policy headline, with the words that decided it."""
    hits: List[str] = []
    up = 0
    for rx in _HAWK:
        m = rx.search(text)
        if m:
            up += 1
            hits.append("+" + m.group(0)[:30])
    for rx in _DOVE:
        m = rx.search(text)
        if m:
            up -= 1
            hits.append("-" + m.group(0)[:30])
    return (max(-1.0, min(1.0, up / 2.0)), hits)


def parse_feed(source: str, raw: bytes) -> List[Dict[str, Any]]:
    text = raw.decode("utf-8-sig", errors="replace")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return []  # never expand entities from a remote document
    root = ET.fromstring(text)
    out: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
        when = item.findtext("pubDate") or ""
        try:
            ts = parsedate_to_datetime(when.strip()).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError):
            ts = None
        if not title:
            continue
        policy = bool(POLICY.search(title + " " + desc))
        tone, words = score_text(title + " " + desc) if policy else (0.0, [])
        out.append({"source": source, "title": title[:240], "url": link if link.startswith("https://") else None,
                    "published": ts, "policy": policy, "tone": tone, "matched": words})
    return out


def blend_tone(items: List[Dict[str, Any]], now: Optional[float] = None) -> Tuple[Optional[float], int]:
    """Recency-weighted mean tone of policy items that matched at least one word."""
    now = now or time.time()
    num = den = 0.0
    n = 0
    for it in items:
        if not it["policy"] or not it["matched"] or not it["published"]:
            continue
        age = max(0.0, now - datetime.fromisoformat(it["published"]).timestamp()) / 86400
        if age > 60:
            continue
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        num += w * it["tone"]
        den += w
        n += 1
    # A neutral prior worth one fresh item: a single headline cannot swing the read all the way.
    return (round(num / (den + 1.0), 3) if den else None, n)


def _newswire() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for name, url in FEEDS.items():
        try:
            items.extend(parse_feed(name, _fetch(url)))
        except Exception as exc:  # one bank down must not hide the others
            errors[name] = str(exc)[:80]
    items.sort(key=lambda x: x["published"] or "", reverse=True)
    tone, n = blend_tone(items)
    return {"tone": tone, "scored": n, "items": items[:40], "errors": errors, "at": int(time.time()),
            "method": "keyword score on monetary-policy headlines (+ hawkish, - dovish), 10-day half-life, shrunk toward neutral when few items match; the matched words are listed on each item"}


@router.get("/api/newswire")
def api_newswire(response: Response) -> Dict[str, Any]:
    data = _cached("newswire", 600, _newswire)
    if not data["items"]:
        raise HTTPException(503, "Central-bank feeds are unreachable right now.")
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=600"
    return data


# ------------------------------------------------------------ cross-venue
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
STOP = set("will the a an of in on to by be is for and or at with than more less before after 2025 2026 2027 does do who what which".split())


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower()) if w not in STOP and len(w) > 1}


def _numbers(text: str) -> set:
    return set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    na, nb = _numbers(a), _numbers(b)
    if (na or nb) and na != nb:
        jac *= 0.4  # different thresholds or dates are different questions
    return jac


def kalshi_fee(price: float) -> float:
    """Kalshi taker fee per contract: 7% x P x (1-P), rounded up to the cent."""
    return math.ceil(0.07 * price * (1 - price) * 100 - 1e-9) / 100


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _kalshi_markets() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor = ""
    for _ in range(4):
        params: Dict[str, Any] = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        res = requests.get(KALSHI + "/events", params=params, headers=_UA, timeout=(4, 12), allow_redirects=False)
        if res.status_code != 200:
            break
        body = res.json()
        for ev in body.get("events") or []:
            mk = [m for m in ev.get("markets") or [] if m.get("market_type") == "binary"]
            for m in mk:
                title = ev.get("title") or ""
                if len(mk) > 1:
                    title = f"{title} {m.get('yes_sub_title') or m.get('title') or ''}"
                yes_ask, no_ask = _f(m.get("yes_ask_dollars")), _f(m.get("no_ask_dollars"))
                if yes_ask is None or no_ask is None or not (0 < yes_ask < 1) or not (0 < no_ask < 1):
                    continue
                out.append({"ticker": m.get("ticker"), "title": title.strip()[:200], "yes_ask": yes_ask, "no_ask": no_ask,
                            "close": m.get("close_time"), "rules": (m.get("rules_primary") or "")[:400],
                            "url": f"https://kalshi.com/markets/{str(ev.get('series_ticker') or '').lower()}"})
        cursor = body.get("cursor") or ""
        if not cursor:
            break
    return out


def _poly_markets(pages: int = 6) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for page in range(pages):  # Gamma returns at most 100 markets per call
        query: Dict[str, Any] = {"active": "true", "closed": "false", "limit": 100, "offset": page * 100, "order": "volume24hr", "ascending": "false"}
        res = requests.get(GAMMA + "/markets", params=query,
                           headers=_UA, timeout=(4, 12), allow_redirects=False)
        if res.status_code != 200:
            break
        batch = res.json() or []
        rows.extend(batch)
        if len(batch) < 100:
            break
    out = []
    for m in rows:
        bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
        if bid is None or ask is None or not (0 < ask < 1) or not (0 < bid < 1):
            continue
        evs = m.get("events") or [{}]
        out.append({"id": str(m.get("id")), "title": str(m.get("question") or "")[:200], "yes_ask": ask, "no_ask": round(1 - bid, 4),
                    "close": m.get("endDate"), "rules": str(m.get("description") or "")[:400],
                    "url": "https://polymarket.com/event/" + str((evs[0] or {}).get("slug") or "")})
    return out


def match_pairs(kalshi: List[Dict[str, Any]], poly: List[Dict[str, Any]], min_sim: float = 0.55, top: int = 25) -> List[Dict[str, Any]]:
    index: Dict[str, List[int]] = {}
    for i, k in enumerate(kalshi):
        for w in _tokens(k["title"]):
            index.setdefault(w, []).append(i)
    pairs = []
    for p in poly:
        cand = {i for w in _tokens(p["title"]) for i in index.get(w, [])}
        best = max(((similarity(p["title"], kalshi[i]["title"]), i) for i in cand), default=(0.0, -1))
        if best[0] < min_sim:
            continue
        k = kalshi[best[1]]
        legs = [("YES on Polymarket + NO on Kalshi", p["yes_ask"], k["no_ask"]), ("YES on Kalshi + NO on Polymarket", k["yes_ask"], p["no_ask"])]
        name, poly_px, kal_px = min(legs, key=lambda x: x[1] + x[2] + kalshi_fee(x[2]))
        cost = poly_px + kal_px + kalshi_fee(kal_px)
        pairs.append({"similarity": round(best[0], 2), "polymarket": p, "kalshi": k, "trade": name, "cost": round(cost, 4),
                      "locked_edge": round(1 - cost, 4), "arb": cost < 1})
    pairs.sort(key=lambda x: (not x["arb"], x["cost"]))
    return pairs[:top]


def _xarb() -> Dict[str, Any]:
    kalshi, poly = _kalshi_markets(), _poly_markets()
    return {"pairs": match_pairs(kalshi, poly), "kalshi_markets": len(kalshi), "polymarket_markets": len(poly), "at": int(time.time()),
            "warning": "Titles are matched automatically. Two venues can word the same question differently and resolve it on different sources, "
                       "dates or edge cases. Read both rule texts before treating any pair as risk-free. Polymarket fees vary by market; "
                       "Kalshi's taker fee is included. Read-only: Loki places no orders on either venue."}


@router.get("/api/xarb")
def api_xarb(response: Response) -> Dict[str, Any]:
    try:
        data = _cached("xarb", 120, _xarb)
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(503, "Kalshi or Polymarket is unreachable right now.") from exc
    if not data["kalshi_markets"] or not data["polymarket_markets"]:
        with _lock:
            _cache.pop("xarb", None)
        raise HTTPException(503, "Kalshi or Polymarket returned no open markets right now.")
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
    return data


# ---------------------------------------------------------------- telemetry
@router.get("/api/status/telemetry")
def api_telemetry(response: Response) -> Dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss_kb = usage.ru_maxrss  # kilobytes on Linux
    rss_now = None
    try:
        with open("/proc/self/statm") as fh:
            rss_now = int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, IndexError):
        pass
    uptime = time.time() - _STARTED
    cpu = usage.ru_utime + usage.ru_stime
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    response.headers["Cache-Control"] = "no-store"
    return {"server_time": time.time(), "uptime_s": round(uptime, 1), "cold_start": uptime < 15,
            "rss_mb": round(rss_now, 1) if rss_now else None, "peak_rss_mb": round(rss_kb / 1024, 1),
            "cpu_s": round(cpu, 2), "cpu_share": round(cpu / uptime, 4) if uptime > 0 else None, "load_1m": load,
            "region": os.getenv("VERCEL_REGION") or "local", "python": platform.python_version()}


# ------------------------------------------------------------- signal relay
RELAY_TARGETS = (
    re.compile(r"^https://api\.telegram\.org/bot\d{5,12}:[A-Za-z0-9_-]{30,50}/sendMessage$"),
    re.compile(r"^https://discord\.com/api/webhooks/\d{10,25}/[A-Za-z0-9_-]{40,100}$"),
    re.compile(r"^https://hooks\.slack\.com/services/[A-Z0-9]{6,15}/[A-Z0-9]{6,15}/[A-Za-z0-9]{16,40}$"),
)


class RelayIn(BaseModel):
    url: str = Field(min_length=20, max_length=300)
    text: str = Field(min_length=1, max_length=1200)
    chat_id: Optional[str] = Field(default=None, max_length=40, pattern=r"^-?@?[A-Za-z0-9_]{1,40}$")


@router.post("/api/signals/relay")
def api_signal_relay(payload: RelayIn) -> Dict[str, Any]:
    url = payload.url.strip()
    if not any(rx.match(url) for rx in RELAY_TARGETS):
        raise HTTPException(422, "Only a Telegram bot sendMessage URL, a Discord webhook or a Slack webhook can be used.")
    if "telegram" in url:
        if not payload.chat_id:
            raise HTTPException(422, "Telegram needs a chat id.")
        body: Dict[str, Any] = {"chat_id": payload.chat_id, "text": payload.text, "disable_web_page_preview": True}
    elif "discord" in url:
        body = {"content": payload.text, "allowed_mentions": {"parse": []}}
    else:
        body = {"text": payload.text}
    try:
        res = requests.post(url, json=body, timeout=(4, 8), allow_redirects=False, headers={"User-Agent": _UA["User-Agent"]})
    except requests.RequestException as exc:
        raise HTTPException(502, "The signal service could not be reached.") from exc
    if res.status_code >= 300:
        raise HTTPException(502, f"The signal service answered HTTP {res.status_code}.")
    return {"ok": True, "delivered": True}

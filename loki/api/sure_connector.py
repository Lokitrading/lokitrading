"""Sure connector: your real money picture inside Loki, read by Loki's AI.

Sure (https://github.com/we-promise/sure, the community fork of Maybe Finance) is a self-hosted
personal-finance app: accounts, balances, net worth and investment holdings. It publishes an HTTP
API; Loki talks to it over that API only, so no Sure code is copied into Loki (Sure is AGPL-3.0,
Loki is not) and each app keeps its own licence.

What this module does:

* ``GET /api/sure/overview`` reads your Sure server's balance sheet, accounts and latest
  holdings, then puts Loki's Portfolio AI next to every holding Loki covers (its trend score and
  regime, and whether the AI's own book holds it), plus a few plain facts about the mix
  (largest holding, crypto and cash shares). Facts and readings, never advice.

How it stays safe:

* Read-only: only fixed GET paths on Sure's ``/api/v1`` are ever requested; nothing is written.
* The Sure address and API key come from your browser with each request (headers
  ``X-Sure-Url`` and ``X-Sure-Key``) and are never stored, logged or echoed by the server.
* No server-side request forgery: on the hosted site the address must be https on a public
  internet address (private, loopback, link-local and reserved ranges are refused) and redirects
  are not followed. The desktop app (``LOKI_DESKTOP=1``) also allows your own computer and home
  network, where a self-hosted Sure usually runs. Link-local addresses (cloud metadata) are
  always refused.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Header, HTTPException, Response

router = APIRouter(prefix="/api/sure", tags=["sure"])

TIMEOUT_S = 15
MAX_BYTES = 2_000_000  # per Sure answer
PAGE_SIZE = 100
MAX_PAGES = 5  # 500 accounts or holdings is far more than a household has
CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LTC", "LINK", "BNB", "AVAX", "SHIB", "PEPE", "BONK", "WIF"}


class SureError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _desktop() -> bool:
    return os.environ.get("LOKI_DESKTOP") == "1" or os.environ.get("LOKI_SURE_ALLOW_PRIVATE") == "1"


def _addresses(host: str) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise SureError(400, f"Could not find a server called {host}.")
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


def base_url(raw: Optional[str]) -> str:
    """The checked root address of a Sure server (scheme://host[:port], no path, no credentials)."""
    text = (raw or "").strip()
    if not text:
        raise SureError(400, "Enter your Sure server's address, for example https://sure.example.com.")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise SureError(400, "That Sure address is not a valid web address.")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise SureError(400, "Use a plain http(s) address for your Sure server, without a user name or password in it.")
    host = parts.hostname
    addresses = _addresses(host)
    private = [a for a in addresses if not a.is_global]
    if any(a.is_link_local or a.is_multicast or a.is_unspecified for a in addresses):
        raise SureError(400, "That address is not allowed.")
    if private and not _desktop():
        raise SureError(400, "On the website Loki can only reach a Sure server on the public internet. "
                             "To use one on your own computer or home network, use the Loki desktop app.")
    if parts.scheme == "http" and not private:
        raise SureError(400, "Use https for a Sure server on the internet, so your API key is encrypted on the way.")
    netloc = host if ":" not in host else f"[{host}]"
    return f"{parts.scheme}://{netloc}{f':{port}' if port else ''}"


def _get(base: str, key: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        reply = requests.get(base + path, params=params, timeout=TIMEOUT_S, allow_redirects=False, stream=True,
                             headers={"X-Api-Key": key, "Accept": "application/json", "User-Agent": "Loki-Sure-connector"})
    except requests.RequestException as error:
        raise SureError(502, f"Could not reach your Sure server ({type(error).__name__}).")
    try:
        if reply.status_code in (401, 403):
            raise SureError(401, "Your Sure server refused the API key. Create a read key in Sure under Settings, API key.")
        if 300 <= reply.status_code < 400:
            raise SureError(502, "Your Sure server answered with a redirect; enter its final address.")
        if reply.status_code != 200:
            raise SureError(502, f"Your Sure server answered {reply.status_code} for {path}.")
        body = reply.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(body) > MAX_BYTES:
            raise SureError(502, "Your Sure server sent more data than Loki reads at once.")
        try:
            import json

            return json.loads(body)
        except ValueError:
            raise SureError(502, "That address answered, but not like a Sure server.")
    finally:
        reply.close()


def _pages(base: str, key: str, path: str, field: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        data = _get(base, key, path, {**(params or {}), "page": page, "per_page": PAGE_SIZE})
        batch = data.get(field) if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise SureError(502, "That address answered, but not like a Sure server.")
        rows.extend(r for r in batch if isinstance(r, dict))
        total_pages = ((data.get("pagination") or {}).get("total_pages")) or 1
        if page >= int(total_pages):
            break
    return rows


def _num(value: Any) -> Optional[float]:
    """A number from Sure's JSON: plain numbers, or formatted money such as "$60,000.00",
    "A$1,234.50", "€1.234,56" or "-$12.00" (Sure formats amounts in the account's currency)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("-") or text.startswith("(") or "-" in text[:3]
    digits = "".join(ch for ch in text if ch.isdigit() or ch in ".,")
    if not any(ch.isdigit() for ch in digits):
        return None
    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    if last_dot >= 0 and last_comma >= 0:  # both: the later one is the decimal mark
        decimal = "." if last_dot > last_comma else ","
    elif last_comma >= 0:  # only commas: a decimal mark when 1-2 digits follow the last one
        decimal = "," if len(digits) - last_comma - 1 in (1, 2) and digits.count(",") == 1 else ""
    else:
        decimal = "." if digits.count(".") == 1 else ""
    thousands = {".", ","} - {decimal}
    cleaned = "".join(ch for ch in digits if ch not in thousands)
    if decimal == ",":
        cleaned = cleaned.replace(",", ".")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def latest_holdings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sure lists a holding per account, security and day: keep each one's most recent day."""
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for h in rows:
        sec = h.get("security") or {}
        acct = h.get("account") or {}
        key = (str(acct.get("id", "")), str(sec.get("id") or sec.get("ticker") or h.get("id")))
        if key not in best or str(h.get("date", "")) > str(best[key].get("date", "")):
            best[key] = h
    return list(best.values())


def loki_symbol(ticker: str, covered: Dict[str, Dict[str, Any]]) -> Optional[str]:
    t = (ticker or "").upper().strip()
    for candidate in (t, f"{t}-USD", t.replace("-USD", "")):
        if candidate in covered:
            return candidate
    return None


def analyse(sheet: Dict[str, Any], accounts: List[Dict[str, Any]], holdings: List[Dict[str, Any]],
            positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Put Loki's Portfolio AI next to your holdings, and state plain facts about the mix."""
    covered = {str(p.get("symbol", "")).upper(): p for p in positions if p.get("symbol")}
    rows: List[Dict[str, Any]] = []
    for h in latest_holdings(holdings):
        sec = h.get("security") or {}
        ticker = str(sec.get("ticker") or "").upper()
        value = _num(h.get("amount"))
        sym = loki_symbol(ticker, covered)
        read: Optional[Dict[str, Any]] = None
        if sym:
            p = covered[sym]
            score = _num(p.get("score"))
            read = {"symbol": sym, "trend_score": score, "regime": p.get("regime"),
                    "ai_holds": bool((p.get("weight") or 0) > 0), "ai_weight_pct": round(float(p.get("weight") or 0) * 100, 1),
                    "trend": None if score is None else ("uptrend" if score > 0 else "downtrend" if score < 0 else "flat")}
        rows.append({"ticker": ticker or None, "name": sec.get("name"), "qty": _num(h.get("qty")), "price": _num(h.get("price")),
                     "value": value, "currency": h.get("currency"), "account": (h.get("account") or {}).get("name"),
                     "as_of": h.get("date"), "loki": read})
    rows.sort(key=lambda r: -(r["value"] or 0))

    def balance(a: Dict[str, Any]) -> Optional[float]:
        cents = _num(a.get("balance_cents"))
        return cents / 100 if cents is not None else _num(a.get("balance"))

    accts: List[Dict[str, Any]] = [{"name": a.get("name"), "type": a.get("account_type"), "classification": a.get("classification"),
              "balance": balance(a),
              "currency": a.get("currency"), "institution": a.get("institution_name")}
             for a in accounts if not a.get("exclude_from_reports") and a.get("status") not in ("disabled", "pending_deletion")]
    accts.sort(key=lambda a: -(abs(a["balance"] or 0)))

    invested = sum(r["value"] or 0 for r in rows)
    assets = sum(a["balance"] or 0 for a in accts if a["classification"] == "asset")
    cash = sum(a["balance"] or 0 for a in accts if a["classification"] == "asset" and str(a["type"]).lower() in ("depository", "cash"))
    crypto = sum(r["value"] or 0 for r in rows if (r["ticker"] or "").replace("-USD", "") in CRYPTO_TICKERS)
    top = rows[0] if rows and invested > 0 else None
    covered_rows = [r for r in rows if r["loki"]]
    down = [r for r in covered_rows if r["loki"]["trend"] == "downtrend"]

    facts: List[str] = []
    if top:
        share = 100 * (top["value"] or 0) / invested
        facts.append(f"Your largest holding, {top['ticker'] or top['name']}, is {share:.0f}% of your investments.")
    if invested > 0 and crypto > 0:
        facts.append(f"Crypto is {100 * crypto / invested:.0f}% of your investments.")
    if assets > 0:
        facts.append(f"Cash accounts hold {100 * cash / assets:.0f}% of your assets.")
    if covered_rows:
        facts.append(f"Loki's AI covers {len(covered_rows)} of your {len(rows)} holdings; "
                     f"{len(down)} of those are in a downtrend by its trend score.")
        held = [r["loki"]["symbol"] for r in covered_rows if r["loki"]["ai_holds"]]
        if held:
            facts.append("Loki's Portfolio AI also holds " + ", ".join(sorted(set(held))) + " in its own simulated book.")
    elif rows and positions:
        facts.append("None of your holdings are in the 21 markets Loki's AI follows, so it has no reading for them.")

    def money(block: Any) -> Optional[float]:
        return _num(block.get("amount")) if isinstance(block, dict) else _num(block)

    return {
        "currency": sheet.get("currency"),
        "net_worth": money(sheet.get("net_worth")),
        "assets": money(sheet.get("assets")),
        "liabilities": money(sheet.get("liabilities")),
        "accounts": accts,
        "holdings": rows,
        "invested": round(invested, 2),
        "facts": facts,
        "as_of": date.today().isoformat(),
        "note": "Read from your Sure server just now. Loki's readings are simulated research, not advice.",
    }


AI_WAIT_S = 6.0  # the Portfolio AI's first build can take a while; your money never waits longer than this


def _loki_positions(wait_s: float = AI_WAIT_S) -> List[Dict[str, Any]]:
    """The Portfolio AI's current reading per market, or [] if it is not ready within ``wait_s``.

    A slow first build carries on in the background, so the next read has it.
    """
    import threading

    box: Dict[str, Any] = {}

    def build() -> None:
        try:
            from loki.api import ai_paper

            box["plan"] = ai_paper.ai_portfolio(Response())
        except Exception:  # noqa: BLE001 - the overview still shows your money without Loki's reading
            box["plan"] = None

    worker = threading.Thread(target=build, name="sure-ai-reading", daemon=True)
    worker.start()
    worker.join(wait_s)
    plan = box.get("plan")
    return list(plan.get("positions") or []) if isinstance(plan, dict) else []


@router.get("/local")
def sure_local(response: Response) -> Dict[str, Any]:
    """In the desktop app: is Sure running on this computer, and where? (The app starts it.)"""
    response.headers["Cache-Control"] = "no-store"
    if os.environ.get("LOKI_DESKTOP") != "1":
        return {"desktop": False}
    url = os.environ.get("LOKI_SURE_LOCAL_URL") or ""
    state = os.environ.get("LOKI_SURE_LOCAL") or "off"
    if url and state in ("checking", "starting", "running"):
        try:
            up = requests.get(url + "/up", timeout=1.5, allow_redirects=False).status_code == 200
        except requests.RequestException:
            up = False
        state = "running" if up else ("starting" if state != "checking" else "checking")
    return {"desktop": True, "url": url or None, "state": state}


@router.get("/overview")
def sure_overview(response: Response, x_sure_url: Optional[str] = Header(None),
                  x_sure_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Your Sure net worth, accounts and holdings, with Loki's AI reading beside each holding."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        base = base_url(x_sure_url)
        key = (x_sure_key or "").strip()
        if not key or len(key) > 512:
            raise SureError(400, "Paste the API key from your Sure server (Settings, API key).")
        sheet = _get(base, key, "/api/v1/balance_sheet")
        if not isinstance(sheet, dict) or "net_worth" not in sheet:
            raise SureError(502, "That address answered, but not like a Sure server.")
        accounts = _pages(base, key, "/api/v1/accounts", "accounts")
        holdings = _pages(base, key, "/api/v1/holdings", "holdings")
    except SureError as error:
        raise HTTPException(error.status, error.detail)
    positions = _loki_positions()
    result = analyse(sheet, accounts, holdings, positions)
    if not positions:
        result["facts"].append("Loki's AI reading is still warming up; refresh in a minute to see it beside your holdings.")
    result["server"] = urlsplit(base).hostname
    return result

"""Sending an order to Alpaca exactly once, even when its answer is lost.

Every order carries a fixed client order id (built from the signal, so a retry reuses it). When
Alpaca's answer to a POST is lost (a timeout or a 5xx), the order may or may not have landed.
Instead of guessing, ``submit_once`` asks Alpaca for the order by that id:

* found: the order landed; it is reported with its real status;
* Alpaca says it has no such order: nothing was placed; the next check sends it again with the
  same id, which Alpaca would refuse as a duplicate if the first copy turned up after all;
* the lookup itself fails: the result is reported as unknown, and the next check's reconciliation
  (it re-reads positions and orders before deciding anything) settles it.

A 422 naming the client order id means Alpaca already has the order: reported as already sent.
"""
from __future__ import annotations

from typing import Any, Callable, Dict
from urllib.parse import quote

from fastapi import HTTPException

from loki.api.broker_alpaca import AlpacaPaperKeys, _order_view

Exchange = Callable[..., Any]
LOST = (502, 503, 504)


def submit_once(exchange: Exchange, keys: AlpacaPaperKeys, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST an order; if the answer is lost, look the order up by its client id before reporting."""
    cid = str(body["client_order_id"])
    try:
        payload = exchange("POST", "/v2/orders", keys, body)
        return {**(_order_view(payload) if isinstance(payload, dict) else {}), "client_order_id": cid}
    except HTTPException as error:
        if error.status_code == 422 and "client_order_id" in str(error.detail):
            return {"client_order_id": cid, "status": "already sent"}  # a retry: Alpaca has this order
        if error.status_code not in LOST:
            raise
    try:
        found = exchange("GET", f"/v2/orders:by_client_order_id?client_order_id={quote(cid)}", keys)
    except HTTPException as error:
        if error.status_code == 401:
            raise
        if "404" in str(error.detail):
            raise HTTPException(503, "Alpaca's answer was lost and it has no record of this order, so nothing was placed. "
                                     "The next check sends it again with the same id, so it cannot be placed twice.") from error
        raise HTTPException(503, "Alpaca's answer was lost and the order could not be looked up. The next check re-reads "
                                 "the account before deciding and reuses the same id, so it cannot be placed twice.") from error
    if isinstance(found, dict) and found.get("id"):
        return {**_order_view(found), "client_order_id": cid, "verified": "answer lost; Alpaca confirms the order landed"}
    raise HTTPException(503, "Alpaca's answer was lost and it has no record of this order, so nothing was placed. "
                             "The next check sends it again with the same id, so it cannot be placed twice.")

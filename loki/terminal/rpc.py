"""Solana RPC with fallback routing: several endpoints, health-scored, failing over automatically.

Endpoints come from ``SOLANA_RPC_URLS`` (comma-separated https URLs, e.g. a Helius/Triton key
first) plus the public mainnet endpoint last. Each call goes to the healthiest endpoint; an
error or timeout marks it down for ``COOLDOWN_S`` and the call is retried on the next one.
Latency is tracked so the fastest healthy endpoint is preferred.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
COOLDOWN_S = 30.0
TIMEOUT_S = 6.0


class RpcError(RuntimeError):
    pass


class RpcPool:
    def __init__(self, urls: Optional[List[str]] = None) -> None:
        extra = [u.strip() for u in os.environ.get("SOLANA_RPC_URLS", "").split(",") if u.strip().startswith("https://")]
        self.urls = list(dict.fromkeys((urls if urls is not None else extra) + [PUBLIC_RPC]))
        self.down_until: Dict[str, float] = {}
        self.latency: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._id = 0

    def order(self, now: Optional[float] = None) -> List[str]:
        now = time.time() if now is None else now
        with self._lock:
            up = [u for u in self.urls if self.down_until.get(u, 0) <= now]
            down = [u for u in self.urls if u not in up]
            up.sort(key=lambda u: (self.urls.index(u) if u not in self.latency else 0, self.latency.get(u, 9e9)))
        return up + down  # every endpoint is tried before giving up

    def call(self, method: str, params: List[Any], timeout: float = TIMEOUT_S) -> Any:
        errors = []
        for url in self.order():
            with self._lock:
                self._id += 1
                rid = self._id
            started = time.time()
            try:
                r = requests.post(url, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                                  timeout=timeout, allow_redirects=False)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RpcError(f"HTTP {r.status_code}")
                body = r.json()
                if "error" in body:
                    # A request error (bad params) is not the endpoint's fault: report it, don't fail over.
                    raise ValueError(str(body["error"].get("message", body["error"])))
                with self._lock:
                    self.latency[url] = 0.7 * self.latency.get(url, time.time() - started) + 0.3 * (time.time() - started)
                return body.get("result")
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001 - mark down and fail over
                with self._lock:
                    self.down_until[url] = time.time() + COOLDOWN_S
                errors.append(f"{url.split('//')[-1].split('/')[0]}: {type(error).__name__}")
        raise RpcError("Every Solana RPC endpoint failed: " + "; ".join(errors))

    def status(self) -> List[Dict[str, Any]]:
        now = time.time()
        return [{"host": u.split("//")[-1].split("/")[0].split("?")[0], "up": self.down_until.get(u, 0) <= now,
                 "latency_ms": round(1000 * self.latency[u]) if u in self.latency else None} for u in self.urls]


_pool: Optional[RpcPool] = None


def pool() -> RpcPool:
    global _pool
    if _pool is None:
        _pool = RpcPool()
    return _pool

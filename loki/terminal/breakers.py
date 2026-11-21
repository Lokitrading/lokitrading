"""Circuit breakers: the terminal stops itself before a bad day becomes a disaster.

* daily drawdown: realised net P&L (AUD, after all costs) at or below ``-max_daily_loss_aud``
  trips the breaker until the next day (Australia/Sydney trading day by default);
* failed transactions: ``max_consecutive_failures`` failed or expired transactions in a row
  trips the kill switch (a broken RPC, a congested chain or a bug must not keep firing);
* exposure caps: per-trade size and number of open positions;
* manual kill switch (and "Nuke all") trips it too.

A tripped breaker blocks every new entry; exits are always allowed. Only a person resets it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
    _TZ: Any = ZoneInfo("Australia/Sydney")
except Exception:  # noqa: BLE001 - tzdata missing: fall back to UTC days
    _TZ = None


@dataclass
class Limits:
    max_daily_loss_aud: float = 100.0
    max_consecutive_failures: int = 3
    max_trade_aud: float = 50.0
    max_open_positions: int = 3


@dataclass
class Breaker:
    limits: Limits = field(default_factory=Limits)
    day: str = ""
    realised_aud: float = 0.0
    failures_in_row: int = 0
    tripped: Optional[str] = None
    open_positions: int = 0

    def _today(self, now: float) -> str:
        return datetime.fromtimestamp(now, _TZ).strftime("%Y-%m-%d") if _TZ else datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")

    def _roll(self, now: float) -> None:
        today = self._today(now)
        if today != self.day:
            self.day, self.realised_aud = today, 0.0
            if self.tripped and self.tripped.startswith("daily loss"):
                self.tripped = None  # a new trading day; failure and manual trips stay until reset

    def can_enter(self, size_aud: float, now: float) -> Dict[str, Any]:
        self._roll(now)
        why = None
        if self.tripped:
            why = f"breaker tripped: {self.tripped}"
        elif size_aud <= 0:
            why = "trade size must be positive"
        elif size_aud > self.limits.max_trade_aud:
            why = f"trade size A${size_aud:.2f} is over the A${self.limits.max_trade_aud:.2f} cap"
        elif self.open_positions >= self.limits.max_open_positions:
            why = f"already {self.open_positions} open positions (cap {self.limits.max_open_positions})"
        elif self.realised_aud - size_aud < -self.limits.max_daily_loss_aud:
            why = "this trade could take the day past the daily loss limit"
        return {"ok": why is None, "why": why}

    def record_close(self, net_aud: float, now: float) -> None:
        self._roll(now)
        self.realised_aud += net_aud
        self.open_positions = max(0, self.open_positions - 1)
        if self.realised_aud <= -self.limits.max_daily_loss_aud:
            self.tripped = f"daily loss A${-self.realised_aud:.2f} reached the A${self.limits.max_daily_loss_aud:.2f} limit"

    def record_open(self) -> None:
        self.open_positions += 1

    def record_tx(self, ok: bool) -> None:
        self.failures_in_row = 0 if ok else self.failures_in_row + 1
        if self.failures_in_row >= self.limits.max_consecutive_failures:
            self.tripped = f"{self.failures_in_row} failed transactions in a row"

    def kill(self, reason: str = "manual kill switch") -> None:
        self.tripped = reason

    def reset(self) -> None:
        """Only a person calls this."""
        self.tripped, self.failures_in_row = None, 0

    def status(self, now: float) -> Dict[str, Any]:
        self._roll(now)
        return {"day": self.day, "realised_aud": round(self.realised_aud, 2), "failures_in_row": self.failures_in_row,
                "tripped": self.tripped, "open_positions": self.open_positions,
                "limits": vars(self.limits)}

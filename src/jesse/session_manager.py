"""
Market Session Manager for forex/crypto session awareness.

Provides session definitions, active session detection, volatility hints,
and session change event support for autonomous trading decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class SessionName(Enum):
    SYDNEY = "Sydney"
    TOKYO = "Tokyo"
    LONDON = "London"
    NEW_YORK = "New York"
    CRYPTO_247 = "Crypto 24/7"


@dataclass(frozen=True)
class SessionInfo:
    name: SessionName
    is_active: bool
    opens_at: str
    closes_at: str
    typical_volatility: str
    overlap_with: list[SessionName] = field(default_factory=list)


class SessionManager:
    """
    Determines which forex/crypto sessions are active at a given time.

    Session definitions (UTC):
        Sydney:   22:00 – 07:00  (low)
        Tokyo:    00:00 – 09:00  (medium)
        London:   08:00 – 17:00  (high)
        New York: 13:00 – 22:00  (high)
        Crypto:   24/7           (medium)
    """

    _SESSION_DEFS: tuple[dict, ...] = (
        {
            "name": SessionName.SYDNEY,
            "opens": time(22, 0),
            "closes": time(7, 0),
            "volatility": "low",
            "overlap": [SessionName.TOKYO],
        },
        {
            "name": SessionName.TOKYO,
            "opens": time(0, 0),
            "closes": time(9, 0),
            "volatility": "medium",
            "overlap": [SessionName.SYDNEY],
        },
        {
            "name": SessionName.LONDON,
            "opens": time(8, 0),
            "closes": time(17, 0),
            "volatility": "high",
            "overlap": [SessionName.NEW_YORK],
        },
        {
            "name": SessionName.NEW_YORK,
            "opens": time(13, 0),
            "closes": time(22, 0),
            "volatility": "high",
            "overlap": [SessionName.LONDON],
        },
        {
            "name": SessionName.CRYPTO_247,
            "opens": time(0, 0),
            "closes": time(0, 0),  # sentinel: opens == closes means 24/7
            "volatility": "medium",
            "overlap": [],
        },
    )

    _SYMBOL_SESSION_MAP: dict[str, SessionName] = {
        "JPY": SessionName.TOKYO,
        "GBP": SessionName.LONDON,
        "EUR": SessionName.LONDON,
        "USD": SessionName.NEW_YORK,
        "AUD": SessionName.SYDNEY,
        "NZD": SessionName.SYDNEY,
        "BTC": SessionName.CRYPTO_247,
        "ETH": SessionName.CRYPTO_247,
    }

    _VOLATILITY_RANK = {"low": 0, "medium": 1, "high": 2}

    def __init__(self, timezone: str = "UTC") -> None:
        self._tz = ZoneInfo(timezone)
        logger.info("SessionManager initialized with timezone=%s", timezone)

    def get_all_sessions(self) -> list[SessionInfo]:
        """Return static session definitions (is_active always False)."""
        return [
            SessionInfo(
                name=defn["name"],
                is_active=False,
                opens_at=self._format_time(defn["opens"]),
                closes_at=self._format_time(defn["closes"]),
                typical_volatility=defn["volatility"],
                overlap_with=defn["overlap"],
            )
            for defn in self._SESSION_DEFS
        ]

    def get_active_sessions(self, now: datetime | None = None) -> list[SessionInfo]:
        """Return sessions currently active at *now*."""
        dt = self._ensure_utc(now or datetime.now(timezone.utc))
        active = []
        for defn in self._SESSION_DEFS:
            if self._is_session_active(dt, defn):
                active.append(
                    SessionInfo(
                        name=defn["name"],
                        is_active=True,
                        opens_at=self._format_time(defn["opens"]),
                        closes_at=self._format_time(defn["closes"]),
                        typical_volatility=defn["volatility"],
                        overlap_with=defn["overlap"],
                    )
                )
        logger.debug(
            "Active sessions at %s: %s",
            dt.isoformat(),
            [s.name.value for s in active],
        )
        return active

    def get_primary_session(self, now: datetime | None = None) -> Optional[SessionInfo]:
        """Return the highest-volatility active session, or None."""
        active = self.get_active_sessions(now)
        if not active:
            return None
        return max(
            active, key=lambda s: self._VOLATILITY_RANK.get(s.typical_volatility, 0)
        )

    def is_market_open(self, symbol: str, now: datetime | None = None) -> bool:
        """
        Whether *symbol* typically trades now.

        Crypto symbols always return True.  Forex symbols return True when
        at least one forex session is active (weekends excluded).
        """
        symbol_upper = symbol.upper()

        if any(prefix in symbol_upper for prefix in ("BTC", "ETH")):
            return True

        dt = self._ensure_utc(now or datetime.now(timezone.utc))
        if dt.weekday() >= 5:  # Python: Saturday=5, Sunday=6
            return False

        return len(self.get_active_sessions(dt)) > 0

    def get_next_session_change(
        self, now: datetime | None = None
    ) -> tuple[datetime, SessionName, bool]:
        """
        Return (change_time, session_name, is_opening) for the next session boundary.

        *is_opening* is True when the session is about to open, False when closing.
        """
        dt = self._ensure_utc(now or datetime.now(timezone.utc))
        today = dt.date()

        candidates: list[tuple[datetime, SessionName, bool]] = []

        for defn in self._SESSION_DEFS:
            opens = defn["opens"]
            closes = defn["closes"]

            if opens == closes and defn["name"] == SessionName.CRYPTO_247:
                continue

            opens_dt = self._make_utc(today, opens)
            closes_dt = self._make_utc(today, closes)

            # Overnight sessions (close <= open) span midnight
            is_overnight = closes <= opens

            if is_overnight:
                closes_dt = self._make_utc(today, closes) + timedelta(days=1)
            else:
                if closes_dt <= dt:
                    closes_dt += timedelta(days=1)

            if opens_dt <= dt:
                opens_dt += timedelta(days=1)

            if opens_dt > dt:
                candidates.append((opens_dt, defn["name"], True))

            if closes_dt > dt:
                candidates.append((closes_dt, defn["name"], False))

        if not candidates:
            fallback = self._make_utc(today, time(8, 0)) + timedelta(days=1)
            logger.warning(
                "No session change candidates found; returning fallback London open"
            )
            return (fallback, SessionName.LONDON, True)

        change_time, session_name, is_opening = min(candidates, key=lambda c: c[0])
        logger.info(
            "Next session change: %s at %s (%s)",
            session_name.value,
            change_time.isoformat(),
            "opening" if is_opening else "closing",
        )
        return change_time, session_name, is_opening

    def get_session_volatility_hint(self, now: datetime | None = None) -> str:
        """
        Return an overall volatility hint based on active sessions.

        Returns one of: 'quiet', 'normal', 'active', 'volatile'.
        """
        active = self.get_active_sessions(now)
        if not active:
            return "quiet"

        active_names = {s.name for s in active}
        # London-NY overlap (13:00-17:00 UTC) = highest volatility
        if SessionName.LONDON in active_names and SessionName.NEW_YORK in active_names:
            return "volatile"

        has_high = any(s.typical_volatility == "high" for s in active)
        has_medium = any(s.typical_volatility == "medium" for s in active)

        if has_high:
            return "active"
        if has_medium:
            return "normal"
        return "quiet"

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _format_time(t: time) -> str:
        return t.strftime("%H:%M UTC")

    @staticmethod
    def _make_utc(today, t: time) -> datetime:
        return datetime(
            today.year, today.month, today.day, t.hour, t.minute, tzinfo=timezone.utc
        )

    @staticmethod
    def _is_session_active(dt: datetime, defn: dict) -> bool:
        opens = defn["opens"]
        closes = defn["closes"]
        current_time = dt.time()

        if opens == closes and defn["name"] == SessionName.CRYPTO_247:
            return True

        is_overnight = closes <= opens

        if is_overnight:
            return current_time >= opens or current_time <= closes
        return opens <= current_time < closes

    def get_primary_session_for_symbol(self, symbol: str) -> SessionName | None:
        symbol_upper = symbol.upper()

        # JPY pairs take priority over USD (e.g. USDJPY → Tokyo)
        if "JPY" in symbol_upper:
            return SessionName.TOKYO
        if "BTC" in symbol_upper or "ETH" in symbol_upper:
            return SessionName.CRYPTO_247
        if "AUD" in symbol_upper or "NZD" in symbol_upper:
            return SessionName.SYDNEY
        if "GBP" in symbol_upper or "EUR" in symbol_upper:
            return SessionName.LONDON
        if "USD" in symbol_upper:
            return SessionName.NEW_YORK

        return None

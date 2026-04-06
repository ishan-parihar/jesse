"""NewsEventMonitor — monitors economic calendar and news feeds for high-impact market events.

Detects upcoming and recent economic events, maps news headlines to affected
symbols, and fires MarketEvents for high/critical impact items so Jesse can
avoid entering positions before major announcements or exploit volatility plays.

Graceful degradation: if MCP client methods are unavailable or fail, the
monitor logs a warning and continues without crashing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from jesse.market_event_bus import (
    EventType,
    MarketEvent,
    MarketEventBus,
)

logger = logging.getLogger(__name__)


_SYMBOL_MAP: dict[str, list[str]] = {
    "USD": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500"],
    "EUR": ["EURUSD", "EURGBP", "EURJPY"],
    "GBP": ["GBPUSD", "GBPJPY", "EURGBP"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY"],
    "BTC": ["BTCUSD"],
    "ETH": ["ETHUSD"],
}

_KEYWORD_SYMBOL_MAP: dict[str, list[str]] = {
    "Fed": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500"],
    "FOMC": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500"],
    "Powell": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500"],
    "ECB": ["EURUSD", "EURGBP", "EURJPY"],
    "BOE": ["GBPUSD", "GBPJPY", "EURGBP"],
    "NFP": [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "XAUUSD",
        "US500",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
    ],
    "CPI": [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "XAUUSD",
        "US500",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
    ],
    "PPI": [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "XAUUSD",
        "US500",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
    ],
    "BTC": ["BTCUSD"],
    "Bitcoin": ["BTCUSD"],
    "ETH": ["ETHUSD"],
    "Ethereum": ["ETHUSD"],
    "crypto": ["BTCUSD", "ETHUSD"],
}


@dataclass
class NewsEvent:
    """A single news/economic event that may affect markets."""

    title: str
    symbol: str
    impact: str
    scheduled_time: float
    detected_at: float = field(default_factory=time.time)
    source: str = "news_feed"
    description: str = ""
    processed: bool = False

    @property
    def event_id(self) -> str:
        return f"{self.title}:{self.symbol}:{self.scheduled_time}"


class NewsEventMonitor:
    """Monitors economic calendar and news feeds; fires MarketEvents for high/critical impact items."""

    def __init__(
        self,
        event_bus: MarketEventBus,
        mcp_client: Any,
        check_interval: float = 300.0,
    ) -> None:
        self._event_bus = event_bus
        self._mcp_client = mcp_client
        self._check_interval = check_interval
        self._events: list[NewsEvent] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def _fetch_economic_calendar(self) -> list[dict]:
        try:
            method = getattr(self._mcp_client, "trading_economic_calendar", None)
            if method is None:
                logger.warning(
                    "MCP client has no trading_economic_calendar method — skipping calendar fetch"
                )
                return []

            result = await method(hours_ahead=4.0)

            if isinstance(result, dict):
                events = result.get("events", [])
                if isinstance(events, list):
                    return events
            return []

        except Exception:
            logger.warning("Failed to fetch economic calendar", exc_info=True)
            return []

    async def _fetch_news(self, symbols: list[str]) -> list[dict]:
        try:
            method = getattr(self._mcp_client, "news_fetch", None)
            if method is None:
                logger.warning(
                    "MCP client has no news_fetch method — skipping news fetch"
                )
                return []

            keywords: list[str] = []
            for sym in symbols:
                sym_upper = sym.upper()
                if "EUR" in sym_upper:
                    keywords.append("EUR")
                if "GBP" in sym_upper:
                    keywords.append("GBP")
                if "USD" in sym_upper:
                    keywords.append("USD")
                if "JPY" in sym_upper:
                    keywords.append("JPY")
                if "XAU" in sym_upper or "GOLD" in sym_upper:
                    keywords.extend(["gold", "XAU"])
                if "BTC" in sym_upper:
                    keywords.extend(["Bitcoin", "BTC"])
                if "ETH" in sym_upper:
                    keywords.extend(["Ethereum", "ETH"])

            seen: set[str] = set()
            unique_keywords: list[str] = []
            for kw in keywords:
                if kw not in seen:
                    seen.add(kw)
                    unique_keywords.append(kw)

            if not unique_keywords:
                return []

            result = await method(keywords=unique_keywords, limit=20)

            if isinstance(result, dict):
                items = result.get("items", result.get("articles", []))
                if isinstance(items, list):
                    return items
            return []

        except Exception:
            logger.warning(
                "Failed to fetch news for symbols %s", symbols, exc_info=True
            )
            return []

    def _map_news_to_symbol(self, news_items: list[dict]) -> list[NewsEvent]:
        events: list[NewsEvent] = []

        for item in news_items:
            title = item.get("title", "") or ""
            description = item.get("description", "") or ""
            summary = item.get("summary", "") or ""
            text = f"{title} {description} {summary}".strip()
            if not text.strip():
                continue

            impact = self._detect_impact(text, item)
            scheduled_time = self._extract_time(item)

            affected = self._resolve_symbols(text)

            for sym in affected:
                evt = NewsEvent(
                    title=title,
                    symbol=sym,
                    impact=impact,
                    scheduled_time=scheduled_time,
                    source="news_feed",
                    description=text[:500],
                )
                events.append(evt)

        return events

    def _detect_impact(self, text: str, item: dict) -> str:
        text_upper = text.upper()

        impact_raw = item.get("impact", item.get("impact_level", "")).lower()
        if impact_raw in ("critical", "high", "medium", "low"):
            return impact_raw

        critical_keywords = ["CRITICAL", "EMERGENCY", "RATE DECISION", "WAR", "CRISIS"]
        high_keywords = [
            "NFP",
            "NON-FARM",
            "CPI",
            "PPI",
            "FED",
            "FOMC",
            "POWELL",
            "ECB",
            "BOE",
            "RATE",
            "INFLATION",
            "GDP",
            "UNEMPLOYMENT",
            "RECESSION",
        ]
        medium_keywords = ["PMI", "RETAIL SALES", "TRADE BALANCE", "HOUSING"]

        for kw in critical_keywords:
            if kw in text_upper:
                return "critical"

        for kw in high_keywords:
            if kw in text_upper:
                return "high"

        for kw in medium_keywords:
            if kw in text_upper:
                return "medium"

        return "low"

    def _extract_time(self, item: dict) -> float:
        pub_date = item.get("pubDate", item.get("published_at", item.get("date", "")))
        if pub_date:
            try:
                from datetime import datetime

                if isinstance(pub_date, str):
                    for fmt in (
                        "%Y-%m-%dT%H:%M:%S.%fZ",
                        "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S%z",
                        "%Y-%m-%d %H:%M:%S",
                    ):
                        try:
                            dt = datetime.strptime(
                                pub_date.replace("+00:00", "Z").replace("Z", "+0000"),
                                fmt.replace("%z", "%z"),
                            )
                            return dt.timestamp()
                        except ValueError:
                            continue
            except Exception:
                pass
        return time.time()

    def _resolve_symbols(self, text: str) -> list[str]:
        symbols: set[str] = set()
        text_upper = text.upper()

        for keyword, sym_list in _KEYWORD_SYMBOL_MAP.items():
            if keyword.upper() in text_upper:
                symbols.update(sym_list)

        for currency, sym_list in _SYMBOL_MAP.items():
            if currency in text_upper:
                symbols.update(sym_list)

        if not symbols:
            symbols.update(["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"])

        return sorted(symbols)

    def _should_fire_event(self, event: NewsEvent) -> bool:
        if event.processed:
            return False
        if event.impact not in ("high", "critical"):
            return False
        return True

    async def get_upcoming_events(self, hours_ahead: float = 4.0) -> list[NewsEvent]:
        now = time.time()
        cutoff = now + (hours_ahead * 3600)

        async with self._lock:
            upcoming = [
                e
                for e in self._events
                if e.scheduled_time > now and e.scheduled_time <= cutoff
            ]

        return sorted(upcoming, key=lambda e: e.scheduled_time)

    def get_upcoming_events_sync(self, hours_ahead: float = 4.0) -> list[NewsEvent]:
        now = time.time()
        cutoff = now + (hours_ahead * 3600)
        upcoming = [
            e
            for e in self._events
            if e.scheduled_time > now and e.scheduled_time <= cutoff
        ]
        return sorted(upcoming, key=lambda e: e.scheduled_time)

    async def get_recent_events(self, hours_back: float = 1.0) -> list[NewsEvent]:
        now = time.time()
        cutoff = now - (hours_back * 3600)

        async with self._lock:
            recent = [
                e
                for e in self._events
                if e.scheduled_time <= now and e.scheduled_time >= cutoff
            ]

        return sorted(recent, key=lambda e: e.scheduled_time, reverse=True)

    async def check_for_news(self, symbols: list[str] | None = None) -> list[NewsEvent]:
        if symbols is None:
            symbols = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "ETHUSD"]

        new_events: list[NewsEvent] = []

        calendar_items = await self._fetch_economic_calendar()
        for item in calendar_items:
            evt = self._calendar_item_to_event(item)
            if evt:
                new_events.append(evt)

        news_items = await self._fetch_news(symbols)
        news_events = self._map_news_to_symbol(news_items)
        new_events.extend(news_events)

        fired: list[NewsEvent] = []
        async with self._lock:
            existing_ids = {e.event_id for e in self._events}

            for evt in new_events:
                if evt.event_id in existing_ids:
                    continue
                if self._should_fire_event(evt):
                    evt.processed = True
                    self._events.append(evt)
                    self._fire_market_event(evt)
                    fired.append(evt)
                else:
                    self._events.append(evt)

        if len(self._events) > 500:
            self._events = self._events[-500:]

        return fired

    def _calendar_item_to_event(self, item: dict) -> NewsEvent | None:
        try:
            title = item.get("title", item.get("name", "Economic Event"))
            currency = item.get("currency", item.get("country", ""))
            impact = (item.get("impact", item.get("impact_level", "low"))).lower()

            if impact not in ("low", "medium", "high", "critical"):
                impact = "low"

            scheduled_time = float(item.get("timestamp", item.get("time", time.time())))

            affected_symbols = _SYMBOL_MAP.get(currency.upper(), [])
            if not affected_symbols:
                affected_symbols = self._resolve_symbols(title)

            description = item.get("description", item.get("actual", ""))

            if not affected_symbols:
                affected_symbols = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]

            events = []
            for sym in affected_symbols:
                evt = NewsEvent(
                    title=title,
                    symbol=sym,
                    impact=impact,
                    scheduled_time=scheduled_time,
                    source="economic_calendar",
                    description=description[:500] if description else title,
                )
                events.append(evt)

            return events[0] if events else None

        except (ValueError, KeyError, TypeError):
            logger.warning("Failed to parse calendar item: %s", item, exc_info=True)
            return None

    def _fire_market_event(self, evt: NewsEvent) -> None:
        severity_map = {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "critical": "critical",
        }
        severity = severity_map.get(evt.impact, "medium")

        self._event_bus.emit(
            event_type=EventType.NEWS_EVENT,
            symbol=evt.symbol,
            severity=severity,
            data={
                "title": evt.title,
                "description": evt.description,
                "source": evt.source,
                "affected_symbols": [evt.symbol],
                "impact": evt.impact,
                "scheduled_time": evt.scheduled_time,
            },
        )
        logger.info(
            "Fired NEWS_EVENT: [%s] %s → %s (impact=%s)",
            evt.source,
            evt.title,
            evt.symbol,
            evt.impact,
        )

    def start_monitoring(self) -> asyncio.Task[None]:
        if self._running:
            logger.warning("NewsEventMonitor is already running")
            return self._task  # type: ignore[return-value]

        self._running = True
        self._task = asyncio.create_task(
            self._monitor_loop(),
            name="news-event-monitor",
        )
        logger.info("NewsEventMonitor started (interval=%.0fs)", self._check_interval)
        return self._task

    def stop_monitoring(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            logger.info("NewsEventMonitor stopped")

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self.check_for_news()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in news monitoring cycle")

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                raise

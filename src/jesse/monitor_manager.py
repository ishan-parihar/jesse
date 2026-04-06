"""Market monitor manager — starts/stops monitors independently of heartbeat loop.

Monitors run as background event publishers to MarketEventBus.
The LLM reads their state via tools (get_heartbeat_context, get_volatility_states, etc.)
and the HeartbeatRunner uses context_builder to get market context.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class MarketMonitorManager:
    def __init__(
        self, mcp_client, event_bus=None, symbols: list[str] | None = None
    ) -> None:
        from jesse.market_event_bus import MarketEventBus
        from jesse.price_alert_monitor import PriceAlertMonitor
        from jesse.volatility_monitor import VolatilityMonitor
        from jesse.news_event_monitor import NewsEventMonitor
        from jesse.session_manager import SessionManager

        self._mcp_client = mcp_client
        self._event_bus = event_bus or MarketEventBus.instance()
        self._symbols = symbols or []

        self.price_monitor = PriceAlertMonitor(
            event_bus=self._event_bus, mcp_client=mcp_client
        )
        self.vol_monitor = VolatilityMonitor(
            event_bus=self._event_bus, mcp_client=mcp_client
        )
        self.news_monitor = NewsEventMonitor(
            event_bus=self._event_bus, mcp_client=mcp_client
        )
        self.session_manager = SessionManager()
        self.event_bus = self._event_bus
        self._running = False

    def start(self, symbols: list[str] | None = None) -> None:
        if self._running:
            return
        syms = symbols or self._symbols
        if not syms:
            from jesse.scheduler import get_active_symbols

            syms = get_active_symbols()

        self._symbols = syms
        self._subscribe_events()
        self._start_monitors(syms)
        self._running = True
        logger.info("MarketMonitorManager started (%d symbols)", len(syms))

    def stop(self) -> None:
        self._stop_monitors()
        self._running = False
        logger.info("MarketMonitorManager stopped")

    def get_context(self) -> dict:
        from jesse.heartbeat_engine import HeartbeatEngine

        if hasattr(HeartbeatEngine, "_format_recent_events"):
            return HeartbeatEngine._format_recent_events(
                self._event_bus.get_recent_events(limit=10)
            )
        return {}

    def _subscribe_events(self) -> None:
        from jesse.market_event_bus import EventType

        def _on_event(event):
            pass  # Event bus stores events; monitors publish to it

        for event_type in [
            EventType.PRICE_ALERT,
            EventType.VOLATILITY_SPIKE,
            EventType.VOLATILITY_COMPRESS,
            EventType.NEWS_EVENT,
            EventType.SESSION_CHANGE,
            EventType.TREND_CHANGE,
        ]:
            self._event_bus.subscribe(event_type, _on_event, priority=0)

    def _start_monitors(self, symbols: list[str]) -> None:
        try:
            self.price_monitor.start_monitoring(check_interval=5.0)
        except Exception:
            logger.exception("Failed to start price monitor")
        try:
            self.vol_monitor.start_monitoring(symbols=symbols, check_interval=30.0)
        except Exception:
            logger.exception("Failed to start volatility monitor")
        try:
            self.news_monitor.start_monitoring()
        except Exception:
            logger.exception("Failed to start news monitor")
        logger.info("All monitors started for %d symbols", len(symbols))

    def _stop_monitors(self) -> None:
        for monitor, name in [
            (self.price_monitor, "price"),
            (self.vol_monitor, "volatility"),
            (self.news_monitor, "news"),
        ]:
            try:
                monitor.stop_monitoring()
            except Exception:
                logger.exception("Failed to stop %s monitor", name)

"""HeartbeatEngine — event-driven, adaptive heartbeat for Jesse's autonomous trading agent.

Replaces the dumb APScheduler interval with an adaptive coordinator that:
1) Adjusts check frequency based on market sessions (faster in London/NY, slower in Sydney)
2) Wakes up immediately when price alerts or volatility events fire
3) Checks volatility for squeeze setups
4) Avoids trading during high-impact news
5) Provides rich context to the agent via get_context()

Python stdlib + asyncio only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jesse.market_event_bus import (
    EventType,
    MarketEvent,
    MarketEventBus,
)
from jesse.news_event_monitor import NewsEventMonitor
from jesse.price_alert_monitor import PriceAlertMonitor
from jesse.session_manager import SessionManager
from jesse.volatility_monitor import VolatilityMonitor

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatConfig:
    """Configuration for the HeartbeatEngine."""

    base_interval: float = 60.0
    min_interval: float = 10.0
    max_interval: float = 300.0
    session_accel_multiplier: float = 2.0
    event_cooldown: float = 30.0
    max_events_per_cycle: int = 5
    watched_symbols: list[str] = field(default_factory=list)


class HeartbeatEngine:
    """Central coordinator for all monitors with adaptive heartbeat."""

    def __init__(
        self,
        mcp_client,
        event_bus: MarketEventBus | None = None,
        config: HeartbeatConfig | None = None,
        wake_callback=None,
    ) -> None:
        self.config = config or HeartbeatConfig()
        self.event_bus = event_bus or MarketEventBus.instance()
        self.session_manager = SessionManager()
        self._mcp_client = mcp_client
        self._wake_callback = wake_callback

        self.price_monitor: PriceAlertMonitor | None = None
        self.vol_monitor: VolatilityMonitor | None = None
        self.news_monitor: NewsEventMonitor | None = None

        self.running = False
        self._task: asyncio.Task | None = None
        self._last_event_wakeup: float = 0.0
        self._last_wake_time: float = 0.0
        self._event_count: int = 0
        self._current_interval: float = self.config.base_interval

    def initialize(self, symbols: list[str]) -> None:
        self.config.watched_symbols = list(symbols)

        self.price_monitor = PriceAlertMonitor(
            event_bus=self.event_bus,
            mcp_client=self._mcp_client,
        )
        self.vol_monitor = VolatilityMonitor(
            event_bus=self.event_bus,
            mcp_client=self._mcp_client,
        )
        self.news_monitor = NewsEventMonitor(
            event_bus=self.event_bus,
            mcp_client=self._mcp_client,
        )

        self.subscribe_to_events()

        logger.info(
            "HeartbeatEngine initialized: %d symbols, base_interval=%.0fs",
            len(symbols),
            self.config.base_interval,
        )

    def start(self) -> asyncio.Task:
        if self.running:
            logger.warning("HeartbeatEngine already running")
            return self._task  # type: ignore[return-value]

        if (
            self.price_monitor is None
            or self.vol_monitor is None
            or self.news_monitor is None
        ):
            raise RuntimeError(
                "HeartbeatEngine not initialized. Call initialize(symbols) first."
            )

        self.running = True
        self._task = asyncio.create_task(
            self._heartbeat_loop(),
            name="heartbeat-engine",
        )

        self._start_monitors()

        logger.info(
            "HeartbeatEngine started (base_interval=%.0fs)", self.config.base_interval
        )
        return self._task

    def stop(self) -> None:
        self.running = False
        self._stop_monitors()

        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None

        logger.info("HeartbeatEngine stopped")

    async def heartbeat_cycle(self) -> dict:
        start_time = time.time()
        timestamp = datetime.now(timezone.utc).isoformat()

        active_sessions = self.session_manager.get_active_sessions()
        session_names = [s.name.value for s in active_sessions]

        self._current_interval = self.adjust_interval()

        monitors_checked = 0
        events_fired: list[str] = []

        if self.price_monitor is not None:
            try:
                triggered = await self.price_monitor.check_alerts()
                monitors_checked += 1
                for alert in triggered:
                    events_fired.append(
                        f"PRICE_ALERT: {alert.symbol} {alert.condition} {alert.price}"
                    )
            except Exception:
                logger.exception("Price alert check failed")

        if self.vol_monitor is not None:
            try:
                symbols = self.config.watched_symbols
                for sym in symbols:
                    await self.vol_monitor.update_state(sym)
                fired = self.vol_monitor.check_all(symbols)
                monitors_checked += 1
                events_fired.extend(fired)
            except Exception:
                logger.exception("Volatility check failed")

        if self.news_monitor is not None:
            try:
                await self.news_monitor.check_for_news(self.config.watched_symbols)
                monitors_checked += 1
            except Exception:
                logger.exception("News check failed")

        recent = self.event_bus.get_recent_events(
            limit=self.config.max_events_per_cycle
        )
        for evt in recent:
            if evt.event_type not in (EventType.HEARTBEAT,):
                events_fired.append(str(evt))

        elapsed = time.time() - start_time

        return {
            "interval_used": self._current_interval,
            "active_sessions": session_names,
            "events_fired": len(events_fired),
            "monitors_checked": monitors_checked,
            "timestamp": timestamp,
            "cycle_duration_ms": round(elapsed * 1000, 1),
        }

    def adjust_interval(self) -> float:
        interval = self.config.base_interval

        volatility_hint = self.session_manager.get_session_volatility_hint()

        if volatility_hint in ("volatile", "active"):
            interval = interval / self.config.session_accel_multiplier
        elif volatility_hint == "quiet":
            interval = interval * 2.0

        now = time.time()
        time_since_event = now - self._last_event_wakeup
        if time_since_event < self.config.event_cooldown:
            interval = self.config.min_interval

        return max(self.config.min_interval, min(interval, self.config.max_interval))

    def get_context(self) -> dict:
        active_sessions = self.session_manager.get_active_sessions()
        session_names = [s.name.value for s in active_sessions]

        vol_hint = self.session_manager.get_session_volatility_hint()

        recent_events = self._format_recent_events(
            self.event_bus.get_recent_events(limit=10)
        )

        upcoming_news = self._format_upcoming_news()
        price_alerts = self._format_price_alerts()
        volatility_states = self._format_volatility_states()
        recommended = self.adjust_interval()

        return {
            "active_sessions": session_names,
            "session_volatility_hint": vol_hint,
            "recent_events": recent_events,
            "upcoming_news": upcoming_news,
            "price_alerts": price_alerts,
            "volatility_states": volatility_states,
            "recommended_interval": recommended,
        }

    def subscribe_to_events(self) -> None:
        subscriptions = [
            (EventType.PRICE_ALERT, self.on_event, 1),
            (EventType.VOLATILITY_SPIKE, self.on_event, 1),
            (EventType.VOLATILITY_COMPRESS, self.on_event, 0),
            (EventType.NEWS_EVENT, self.on_event, 1),
            (EventType.SESSION_CHANGE, self.on_event, 0),
            (EventType.TREND_CHANGE, self.on_event, 0),
        ]

        for event_type, callback, priority in subscriptions:
            self.event_bus.subscribe(event_type, callback, priority=priority)

        logger.info("HeartbeatEngine subscribed to %d event types", len(subscriptions))

    def on_event(self, event: MarketEvent) -> None:
        self._last_event_wakeup = time.time()
        self._event_count += 1

        log_map = {
            EventType.PRICE_ALERT: logging.INFO,
            EventType.VOLATILITY_SPIKE: logging.WARNING,
            EventType.VOLATILITY_COMPRESS: logging.INFO,
            EventType.NEWS_EVENT: logging.WARNING,
            EventType.SESSION_CHANGE: logging.INFO,
            EventType.TREND_CHANGE: logging.INFO,
        }

        level = log_map.get(event.event_type, logging.INFO)
        logger.log(
            level,
            "Event [%s]: %s %s (severity=%s, count=%d)",
            event.event_type.value.upper(),
            event.symbol,
            event.data.get("title", event.data.get("description", "")),
            event.severity,
            self._event_count,
        )

        if self._wake_callback and (time.time() - self._last_wake_time) > 60:
            if event.event_type in (
                EventType.PRICE_ALERT,
                EventType.VOLATILITY_SPIKE,
                EventType.NEWS_EVENT,
            ):
                self._last_wake_time = time.time()
                logger.info(
                    "High-priority event [%s] — waking agent immediately",
                    event.event_type.value.upper(),
                )
                self._wake_callback()

    def _start_monitors(self) -> None:
        symbols = self.config.watched_symbols

        if self.price_monitor is not None:
            try:
                self.price_monitor.start_monitoring(check_interval=5.0)
            except Exception:
                logger.exception("Failed to start price monitor")

        if self.vol_monitor is not None:
            try:
                self.vol_monitor.start_monitoring(
                    symbols=symbols,
                    check_interval=30.0,
                )
            except Exception:
                logger.exception("Failed to start volatility monitor")

        if self.news_monitor is not None:
            try:
                self.news_monitor.start_monitoring()
            except Exception:
                logger.exception("Failed to start news monitor")

        logger.info("All monitors started for %d symbols", len(symbols))

    def _stop_monitors(self) -> None:
        if self.price_monitor is not None:
            try:
                self.price_monitor.stop_monitoring()
            except Exception:
                logger.exception("Failed to stop price monitor")

        if self.vol_monitor is not None:
            try:
                self.vol_monitor.stop_monitoring()
            except Exception:
                logger.exception("Failed to stop volatility monitor")

        if self.news_monitor is not None:
            try:
                self.news_monitor.stop_monitoring()
            except Exception:
                logger.exception("Failed to stop news monitor")

        logger.info("All monitors stopped")

    async def _heartbeat_loop(self) -> None:
        try:
            while self.running:
                try:
                    await self.heartbeat_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Heartbeat cycle failed")

                await asyncio.sleep(self._current_interval)

        except asyncio.CancelledError:
            logger.info("Heartbeat loop cancelled")
            raise
        except Exception:
            logger.exception("Heartbeat loop crashed")
            raise

    @staticmethod
    def _format_recent_events(events: list[MarketEvent]) -> list[str]:
        formatted: list[str] = []
        now = time.time()

        for event in events:
            if event.event_type == EventType.HEARTBEAT:
                continue

            age_seconds = now - event.timestamp
            if age_seconds < 60:
                age_str = f"{int(age_seconds)}s ago"
            elif age_seconds < 3600:
                age_str = f"{int(age_seconds / 60)} min ago"
            else:
                age_str = f"{int(age_seconds / 3600)}h ago"

            etype_label = event.event_type.value.upper()
            symbol = event.symbol
            severity = event.severity
            data = event.data

            if event.event_type == EventType.PRICE_ALERT:
                condition = data.get("condition", "crossed")
                price = data.get("price", "?")
                current = data.get("current_price", "?")
                desc = f"{symbol} {condition} {price} (now {current})"
            elif event.event_type in (
                EventType.VOLATILITY_SPIKE,
                EventType.VOLATILITY_COMPRESS,
            ):
                ratio = data.get("ratio", data.get("atr_current", "?"))
                avg = data.get("avg_atr", data.get("atr_average", "?"))
                desc = f"{symbol} ATR {ratio}x normal (avg {avg})"
            elif event.event_type == EventType.NEWS_EVENT:
                title = data.get("title", data.get("description", "news event"))
                desc = f"{title}"
            elif event.event_type == EventType.SESSION_CHANGE:
                desc = data.get("description", f"{symbol} session change")
            elif event.event_type == EventType.TREND_CHANGE:
                desc = data.get("description", f"{symbol} trend change")
            else:
                desc = data.get("description", str(event))

            formatted.append(f"[{age_str}] {etype_label}: {desc} ({severity})")

        return formatted

    def _format_upcoming_news(self) -> list[str]:
        if self.news_monitor is None:
            return ["No news monitor active"]

        try:
            upcoming = self.news_monitor.get_upcoming_events_sync(hours_ahead=12.0)
            if not upcoming:
                return ["No upcoming high-impact events"]

            formatted: list[str] = []
            for evt in upcoming:
                time_str = self._relative_time(evt.scheduled_time)
                formatted.append(
                    f"[{time_str}] {evt.title} ({evt.impact} → {evt.symbol})"
                )
            return formatted
        except Exception:
            return ["Error fetching upcoming news"]

    def _format_price_alerts(self) -> list[str]:
        if self.price_monitor is None:
            return ["No price monitor active"]

        try:
            alerts = self.price_monitor.list_alerts_sync()
            if not alerts:
                return ["No active price alerts"]

            formatted: list[str] = []
            for alert in alerts:
                if not alert.active:
                    continue
                formatted.append(
                    f"{alert.symbol} {alert.condition} {alert.price} "
                    f"(severity={alert.severity}, triggered={alert.triggered})"
                )
            return formatted
        except Exception:
            return ["Error fetching price alerts"]

    def _format_volatility_states(self) -> dict:
        if self.vol_monitor is None:
            return {"error": "No volatility monitor active"}

        try:
            return self.vol_monitor.get_all_states()
        except Exception:
            return {"error": "Error fetching volatility states"}

    @staticmethod
    def _relative_time(timestamp: float) -> str:
        now = time.time()
        diff = timestamp - now

        if diff < 0:
            abs_diff = abs(diff)
            if abs_diff < 60:
                return f"{int(abs_diff)}s ago"
            elif abs_diff < 3600:
                return f"{int(abs_diff / 60)}m ago"
            else:
                return f"{int(abs_diff / 3600)}h ago"
        else:
            if diff < 60:
                return f"in {int(diff)}s"
            elif diff < 3600:
                return f"in {int(diff / 60)}m"
            else:
                return f"in {int(diff / 3600)}h"

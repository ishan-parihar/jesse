"""MarketEventBus — pub/sub event bus for Jesse's autonomous trading agent.

Thread-safe, async-compatible event bus that decouples event producers
(monitors) from consumers (heartbeat engine, agent). Python stdlib only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Categories of market events that monitors can emit."""

    PRICE_ALERT = "price_alert"
    VOLATILITY_SPIKE = "volatility_spike"
    VOLATILITY_COMPRESS = "volatility_compress"
    NEWS_EVENT = "news_event"
    SESSION_CHANGE = "session_change"
    TREND_CHANGE = "trend_change"
    HEARTBEAT = "heartbeat"
    TRADE_EXECUTED = "trade_executed"
    POSITION_ALERT = "position_alert"


@dataclass
class MarketEvent:
    """A single market event with metadata and payload."""

    event_type: EventType
    symbol: str
    timestamp: float = field(default_factory=time.time)
    severity: str = "low"
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"MarketEvent(type={self.event_type.value}, symbol={self.symbol}, "
            f"severity={self.severity}, ts={self.timestamp:.0f}, data={self.data})"
        )


Callback = Callable[[MarketEvent], Awaitable[None] | None]


# ── MarketEventBus ───────────────────────────────────────────────────────────


class MarketEventBus:
    """Thread-safe, async-compatible pub/sub event bus with bounded history.

    Singleton via ``MarketEventBus.instance()``.
    """

    _instance: MarketEventBus | None = None
    _instance_lock = threading.Lock()

    _MAX_HISTORY = 100

    def __init__(self) -> None:
        # subscribers: event_type -> list of (priority, callback)
        self._subscribers: dict[EventType, list[tuple[int, Callback]]] = {}
        self._sub_lock = threading.Lock()

        # Bounded ring buffer of recent events (oldest evicted)
        self._history: deque[MarketEvent] = deque(maxlen=self._MAX_HISTORY)
        self._history_lock = threading.Lock()

        # Async lock for dispatch coordination
        self._async_lock: asyncio.Lock | None = None

    # ── Singleton ────────────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> MarketEventBus:
        """Return the singleton bus, creating it if necessary."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Async lock (lazy) ────────────────────────────────────────────────

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    # ── Subscription ─────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: EventType,
        callback: Callback,
        priority: int = 0,
    ) -> None:
        """Register a listener for *event_type*.

        Higher *priority* callbacks are dispatched first.
        """
        with self._sub_lock:
            bucket = self._subscribers.setdefault(event_type, [])
            bucket.append((priority, callback))
            # Keep sorted descending by priority (highest first)
            bucket.sort(key=lambda x: x[0], reverse=True)
        logger.debug(
            "Subscribed callback %r to %s (priority=%d)",
            callback,
            event_type.value,
            priority,
        )

    def unsubscribe(
        self,
        event_type: EventType,
        callback: Callback,
    ) -> None:
        """Remove a listener for *event_type*."""
        with self._sub_lock:
            bucket = self._subscribers.get(event_type)
            if bucket:
                self._subscribers[event_type] = [
                    (p, cb) for p, cb in bucket if cb is not callback
                ]
        logger.debug(
            "Unsubscribed callback %r from %s",
            callback,
            event_type.value,
        )

    # ── Emission ─────────────────────────────────────────────────────────

    def emit(
        self,
        event_type: EventType,
        symbol: str,
        severity: str,
        data: dict | None = None,
    ) -> None:
        """Construct and fire a ``MarketEvent`` to all subscribers."""
        event = MarketEvent(
            event_type=event_type,
            symbol=symbol,
            severity=severity,
            data=data or {},
        )
        self.emit_event(event)

    def emit_event(self, event: MarketEvent) -> None:
        """Fire a pre-constructed ``MarketEvent``."""
        # Append to history (thread-safe, bounded deque)
        with self._history_lock:
            self._history.append(event)

        logger.debug("Emitted %s", event)

        # Dispatch (sync path — each callback invoked directly)
        self._dispatch_sync(event)

    # ── History ──────────────────────────────────────────────────────────

    def get_recent_events(
        self,
        event_type: EventType | None = None,
        limit: int = 20,
    ) -> list[MarketEvent]:
        """Return recent events, optionally filtered by type."""
        with self._history_lock:
            events = list(self._history)
        if event_type is not None:
            events = [e for e in events if e.event_type is event_type]
        # Most recent first
        return list(reversed(events))[:limit]

    def clear_history(self) -> None:
        """Purge the event log."""
        with self._history_lock:
            self._history.clear()
        logger.debug("Event history cleared")

    # ── Internal dispatch ────────────────────────────────────────────────

    def _dispatch_sync(self, event: MarketEvent) -> None:
        """Route *event* to subscribers in priority order (sync path)."""
        with self._sub_lock:
            callbacks = list(self._subscribers.get(event.event_type, []))

        for _priority, callback in callbacks:
            try:
                result = callback(event)
                # If the callback is a coroutine object, schedule it
                if asyncio.iscoroutine(result):
                    # We're in a sync context — create a task on the running loop
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            self._safe_async_invoke(callback, event),
                            name=f"dispatch-{event.event_type.value}",
                        )
                    except RuntimeError:
                        # No running event loop; log warning
                        logger.warning(
                            "Async callback %r returned but no event loop is running — "
                            "use emit_event from an async context or use a sync callback",
                            callback,
                        )
            except Exception:
                logger.exception(
                    "Error in subscriber callback %r for event %s",
                    callback,
                    event,
                )

    async def _safe_async_invoke(
        self,
        callback: Callback,
        event: MarketEvent,
    ) -> None:
        """Invoke an async callback with per-subscriber error isolation."""
        try:
            await callback(event)  # type: ignore[misc]
        except Exception:
            logger.exception(
                "Error in async subscriber callback %r for event %s",
                callback,
                event,
            )

    async def dispatch(self, event: MarketEvent) -> None:
        """Async dispatch: route *event* to subscribers in priority order.

        Catches exceptions per-subscriber so one failure doesn't block others.
        """
        async with self._get_async_lock():
            with self._sub_lock:
                callbacks = list(self._subscribers.get(event.event_type, []))

        for _priority, callback in callbacks:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    await result  # type: ignore[misc]
                # If not a coroutine, it already ran synchronously
            except Exception:
                logger.exception(
                    "Error in subscriber callback %r for event %s",
                    callback,
                    event,
                )

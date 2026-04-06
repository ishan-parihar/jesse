"""
AgentOrchestrator — Single coordinator for all Jesse agent events.

Replaces fragmented execution where:
- Monitors triggered scheduler cycles directly
- Telegram executed trades via direct MCP calls
- APScheduler ran independent cron jobs

Now ALL events flow through here:
- Cron ticks (pre-market scan, weekend scan, regular heartbeat)
- Monitor events (price alerts, volatility spikes, news events)
- User commands (Telegram close/cancel/modify)
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class EventPriority(Enum):
    """Event priority levels for the orchestrator queue."""

    CRITICAL = 0  # User commands (Telegram) — highest priority
    HIGH = 1  # Monitor events (price alerts, volatility, news)
    NORMAL = 2  # Cron ticks (heartbeat, scheduled scans)
    LOW = 3  # Background maintenance (memory consolidation, EOD review)


class EventSource(Enum):
    """Where the event originated from."""

    USER_COMMAND = "user_command"  # Telegram button/command
    PRICE_ALERT = "price_alert"  # PriceAlertMonitor
    VOLATILITY = "volatility_spike"  # VolatilityMonitor
    NEWS = "news_event"  # NewsEventMonitor
    CRON_HEARTBEAT = "cron_heartbeat"  # Regular heartbeat cycle
    CRON_PREMARKET = "cron_premarket"  # Pre-market scan
    CRON_WEEKEND = "cron_weekend"  # Weekend crypto scan
    CRON_EOD = "cron_eod"  # End-of-day review
    MAINTENANCE = "maintenance"  # Memory consolidation


@dataclass
class OrchestratorEvent:
    """An event to be processed by the orchestrator."""

    source: EventSource
    priority: EventPriority
    event_type: str  # "cycle", "user_close", "user_cancel", "alert", etc.
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default="")  # For deduplication


class AgentOrchestrator:
    """
    Single coordinator that receives ALL events and dispatches to the agent.

    Inspired by openclaw's runReplyAgent() pattern: one entry point, all triggers.
    """

    def __init__(
        self,
        scheduler=None,  # AgentScheduler instance — for trigger_cycle
        mcp_client=None,  # McpClient instance — for direct queries
        circuit_breaker=None,  # CircuitBreaker instance
        trade_monitor_callback=None,
        jesse_agent=None,  # JesseAgent instance — for agent-routed commands
    ):
        self._scheduler = scheduler
        self._mcp_client = mcp_client
        self._circuit_breaker = circuit_breaker
        self._trade_monitor_callback = trade_monitor_callback
        self._jesse_agent = jesse_agent

        # Event queue: priority-sorted deque
        self._event_queue: deque[OrchestratorEvent] = deque()
        self._queue_lock = asyncio.Lock()

        # Concurrency control: only one cycle at a time
        self._cycle_lock = asyncio.Lock()
        self._cycle_in_progress = False

        # Deduplication: track recent event IDs
        self._recent_events: dict[str, float] = {}  # event_id -> timestamp
        self._dedup_window = 300.0  # 5 minutes

        # Statistics
        self._total_events = 0
        self._total_cycles = 0
        self._dropped_events = 0
        self._dlq_discarded = 0  # Events discarded after exhausting retries

        # Running flag
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cycle_in_progress(self) -> bool:
        return self._cycle_in_progress

    async def start(self):
        """Start the orchestrator event loop."""
        self._running = True
        self._processor_task = asyncio.create_task(
            self._process_events_loop(), name="orchestrator-event-processor"
        )
        logger.info("AgentOrchestrator started")

    async def stop(self):
        """Stop the orchestrator and drain remaining events."""
        self._running = False
        if hasattr(self, "_processor_task"):
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        logger.info("AgentOrchestrator stopped")

    async def submit_event(self, event: OrchestratorEvent) -> bool:
        """
        Submit an event to the orchestrator queue.
        Returns False if event was deduplicated.
        """
        self._total_events += 1

        # Deduplication
        if event.event_id:
            now = time.time()
            # Clean old entries
            expired = [
                k
                for k, v in self._recent_events.items()
                if now - v > self._dedup_window
            ]
            for k in expired:
                del self._recent_events[k]

            if event.event_id in self._recent_events:
                self._dropped_events += 1
                logger.debug("Dropped duplicate event: %s", event.event_id)
                return False
            self._recent_events[event.event_id] = now

        # Insert into queue (priority-sorted)
        async with self._queue_lock:
            # Insert in priority order (lower number = higher priority)
            inserted = False
            for i, existing in enumerate(self._event_queue):
                if event.priority.value < existing.priority.value:
                    self._event_queue.insert(i, event)
                    inserted = True
                    break
            if not inserted:
                self._event_queue.append(event)

        logger.info(
            "Event queued: source=%s type=%s priority=%s (queue_size=%d)",
            event.source.value,
            event.event_type,
            event.priority.name,
            len(self._event_queue),
        )
        return True

    async def submit_user_command(self, command_type: str, payload: dict) -> bool:
        """
        Convenience method for user-initiated commands (Telegram).
        command_type: "close_position", "cancel_order", "close_all", etc.
        payload: command-specific data
        """
        event = OrchestratorEvent(
            source=EventSource.USER_COMMAND,
            priority=EventPriority.CRITICAL,
            event_type=command_type,
            payload=payload,
            event_id=f"user_{command_type}_{payload.get('id', '')}_{int(time.time() // 60)}",
        )
        return await self.submit_event(event)

    async def submit_monitor_event(self, monitor_type: str, payload: dict) -> bool:
        """
        Convenience method for monitor-triggered events.
        monitor_type: "price_alert", "volatility_spike", "news_event"
        """
        source_map = {
            "price_alert": EventSource.PRICE_ALERT,
            "volatility_spike": EventSource.VOLATILITY,
            "news_event": EventSource.NEWS,
        }
        event = OrchestratorEvent(
            source=source_map.get(monitor_type, EventSource.NEWS),
            priority=EventPriority.HIGH,
            event_type=monitor_type,
            payload=payload,
            event_id=f"monitor_{monitor_type}_{payload.get('symbol', '')}_{int(time.time() // 60)}",
        )
        return await self.submit_event(event)

    async def submit_cycle_request(self, cycle_type: str, payload: dict) -> bool:
        """
        Convenience method for scheduled cycle requests (cron jobs).
        cycle_type: "heartbeat", "premarket", "weekend", "eod"
        """
        source_map = {
            "heartbeat": EventSource.CRON_HEARTBEAT,
            "premarket": EventSource.CRON_PREMARKET,
            "weekend": EventSource.CRON_WEEKEND,
            "eod": EventSource.CRON_EOD,
        }
        event = OrchestratorEvent(
            source=source_map.get(cycle_type, EventSource.CRON_HEARTBEAT),
            priority=EventPriority.NORMAL,
            event_type=cycle_type,
            payload=payload,
            event_id=f"cron_{cycle_type}_{int(time.time() // 300)}",  # Dedup per 5-minute window
        )
        return await self.submit_event(event)

    async def _process_events_loop(self):
        """Main event processing loop — runs continuously."""
        while self._running:
            event = None
            async with self._queue_lock:
                if self._event_queue:
                    event = self._event_queue.popleft()

            if event is None:
                await asyncio.sleep(0.5)
                continue

            retry_count = event.payload.pop("_retry_count", 0)
            try:
                await self._dispatch_event(event)
            except Exception as exc:
                if retry_count < 3:
                    event.payload["_retry_count"] = retry_count + 1
                    logger.warning(
                        "Event dispatch failed (retry %d/3): %s — re-queuing",
                        retry_count + 1,
                        event.event_id,
                        exc,
                    )
                    async with self._queue_lock:
                        self._event_queue.appendleft(event)
                    await asyncio.sleep(1.0 * (retry_count + 1))
                else:
                    logger.critical(
                        "Event %s discarded after 3 retries: %s",
                        event.event_id,
                        exc,
                    )
                    self._dropped_events += 1
                    self._dlq_discarded += 1

    async def _dispatch_event(self, event: OrchestratorEvent):
        """
        Dispatch a single event to the appropriate handler.
        All dispatch goes through the cycle lock (one at a time).
        """
        async with self._cycle_lock:
            self._cycle_in_progress = True
            try:
                if event.source == EventSource.USER_COMMAND:
                    await self._handle_user_command(event)
                elif event.source in (
                    EventSource.PRICE_ALERT,
                    EventSource.VOLATILITY,
                    EventSource.NEWS,
                ):
                    await self._handle_monitor_event(event)
                elif event.source in (
                    EventSource.CRON_HEARTBEAT,
                    EventSource.CRON_PREMARKET,
                    EventSource.CRON_WEEKEND,
                ):
                    await self._handle_cycle_request(event)
                elif event.source in (EventSource.CRON_EOD, EventSource.MAINTENANCE):
                    await self._handle_background_task(event)
                else:
                    logger.warning("Unknown event source: %s", event.source)

                self._total_cycles += 1
            finally:
                self._cycle_in_progress = False

    async def _handle_user_command(self, event: OrchestratorEvent):
        cmd_type = event.event_type
        payload = event.payload

        logger.info("Processing user command: %s payload=%s", cmd_type, payload)

        # Circuit breaker check
        if self._circuit_breaker:
            ok, reason = self._circuit_breaker.check_all()
            if not ok:
                logger.warning("User command blocked by circuit breaker: %s", reason)
                if self._trade_monitor_callback:
                    await self._trade_monitor_callback(
                        "circuit_breaker_blocked",
                        command=cmd_type,
                        reason=reason,
                    )
                return

        # Dispatch through agent if available
        if self._jesse_agent:
            try:
                result = await self._jesse_agent.run_user_command(
                    cmd_type, payload, chat_id="orchestrator"
                )
                logger.info(
                    "User command executed by agent: %s", result.get("text", "")[:200]
                )
                return
            except Exception as exc:
                logger.error(
                    "Agent user command failed, falling back to direct MCP: %s", exc
                )

        # Fallback: direct MCP execution
        await self._execute_command_directly(cmd_type, payload)

    async def _execute_command_directly(self, cmd_type: str, payload: dict):
        if cmd_type == "close_position":
            pos_id = payload.get("position_id")
            if pos_id and self._mcp_client:
                try:
                    result = await self._mcp_client.close_position(pos_id)
                    logger.info("Position %s closed: %s", pos_id, result)
                except Exception as exc:
                    logger.error("Failed to close position %s: %s", pos_id, exc)

        elif cmd_type == "cancel_order":
            order_id = payload.get("order_id")
            if order_id and self._mcp_client:
                try:
                    result = await self._mcp_client.cancel_order(order_id)
                    logger.info("Order %s cancelled: %s", order_id, result)
                except Exception as exc:
                    logger.error("Failed to cancel order %s: %s", order_id, exc)

        elif cmd_type == "close_all_positions":
            if self._mcp_client:
                try:
                    result = await self._mcp_client.close_all_positions()
                    logger.info("All positions closed: %s", result)
                except Exception as exc:
                    logger.error("Failed to close all positions: %s", exc)

        else:
            logger.warning("Unknown user command: %s", cmd_type)

    async def _handle_monitor_event(self, event: OrchestratorEvent):
        """
        Handle monitor-triggered events (price alerts, volatility, news).
        Triggers an immediate agent cycle.
        """
        logger.info(
            "Monitor event: %s symbol=%s",
            event.source.value,
            event.payload.get("symbol", "N/A"),
        )

        # Trigger immediate agent cycle via scheduler
        if self._scheduler:
            try:
                await self._scheduler.trigger_cycle()
            except Exception as exc:
                logger.error("Failed to trigger cycle for monitor event: %s", exc)
        else:
            logger.warning("No scheduler available for monitor event")

    async def _handle_cycle_request(self, event: OrchestratorEvent):
        """
        Handle scheduled cycle requests (heartbeat, pre-market, weekend).
        Triggers a normal agent cycle.
        """
        logger.info("Cycle request: %s", event.event_type)

        if self._scheduler:
            try:
                await self._scheduler.trigger_cycle()
            except Exception as exc:
                logger.error("Failed to trigger cycle: %s", exc)
        else:
            logger.warning("No scheduler available for cycle request")

    async def _handle_background_task(self, event: OrchestratorEvent):
        """
        Handle background tasks (EOD review, memory consolidation).
        These don't require a full agent cycle.
        """
        logger.info("Background task: %s", event.event_type)

        if event.event_type == "eod":
            await self._run_eod_review()
        elif event.event_type == "maintenance":
            await self._run_maintenance()

    async def _run_eod_review(self):
        """Run end-of-day review (read-only, no trading)."""
        if not self._mcp_client:
            return
        try:
            insights = await self._mcp_client.trading_insights(lookback_days=1)
            logger.info("EOD review complete: %s", insights)
        except Exception as exc:
            logger.error("EOD review failed: %s", exc)

    async def _run_maintenance(self):
        """Run memory consolidation and decay."""
        try:
            from jesse.consolidation import consolidate
            from jesse.decay import apply_decay

            consolidate()
            apply_decay()
            logger.info("Memory maintenance complete")
        except Exception as exc:
            logger.error("Memory maintenance failed: %s", exc)

    def get_stats(self) -> dict:
        """Return orchestrator statistics."""
        return {
            "total_events": self._total_events,
            "total_cycles": self._total_cycles,
            "dropped_events": self._dropped_events,
            "dlq_discarded": self._dlq_discarded,
            "queue_size": len(self._event_queue),
            "cycle_in_progress": self._cycle_in_progress,
            "running": self._running,
        }

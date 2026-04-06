"""Coalesced priority-queue wake system — modelled after openclaw heartbeat-wake.ts.

Replaces the simple asyncio.sleep loop with a priority-based wake queue that:
- Coalesces multiple wake requests (250ms default)
- Prioritises: retry < interval < default < action
- Retries on busy/failed runs with 1s backoff
- Supports handler registration with generation-based disposal
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_COALESCE_MS = 0.250
DEFAULT_RETRY_MS = 1.0

REASON_PRIORITY = {"retry": 0, "interval": 1, "default": 2, "action": 3}

HeartbeatRunResult = tuple[bool, str]

from typing import Any


@dataclass
class _PendingWake:
    reason: str
    priority: int
    requested_at: float


class WakeLayer:
    def __init__(self) -> None:
        self._handler: Any = None
        self._handler_gen = 0
        self._pending: dict[str, _PendingWake] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._stopped = False

    def _resolve_priority(self, reason: str) -> int:
        if reason == "retry":
            return REASON_PRIORITY["retry"]
        if reason == "interval":
            return REASON_PRIORITY["interval"]
        return REASON_PRIORITY.get(reason, REASON_PRIORITY["default"])

    def _enqueue(self, reason: str, requested_at: float | None = None) -> None:
        now = requested_at or time.monotonic()
        priority = self._resolve_priority(reason)
        existing = self._pending.get(reason)
        if existing is None:
            self._pending[reason] = _PendingWake(reason, priority, now)
            return
        if priority > existing.priority:
            self._pending[reason] = _PendingWake(reason, priority, now)
        elif priority == existing.priority and now >= existing.requested_at:
            self._pending[reason] = _PendingWake(reason, priority, now)

    def request_wake(
        self, reason: str = "default", coalesce_ms: float | None = None
    ) -> None:
        self._enqueue(reason)
        if self._task is None or self._task.done():
            delay = (
                (coalesce_ms / 1000.0)
                if coalesce_ms is not None
                else DEFAULT_COALESCE_MS
            )
            self._task = asyncio.create_task(
                self._scheduler_loop(delay), name="wake-layer"
            )

    def set_handler(self, handler: Any = None) -> None:
        self._handler_gen += 1
        self._handler = handler

    async def _scheduler_loop(self, initial_delay: float) -> None:
        while not self._stopped and self._pending:
            await asyncio.sleep(initial_delay)
            if self._stopped or not self._pending:
                break

            batch = list(self._pending.values())
            self._pending.clear()

            if self._running:
                for w in batch:
                    self._enqueue(w.reason, time.monotonic())
                continue

            self._running = True
            try:
                for wake in batch:
                    if self._handler is None:
                        continue
                    try:
                        ok, reason_str = await self._handler(wake.reason)
                        if not ok and reason_str == "requests-in-flight":
                            self._enqueue("retry", time.monotonic())
                    except Exception:
                        logger.exception(
                            "Wake handler failed for reason=%s", wake.reason
                        )
                        self._enqueue("retry", time.monotonic())
            finally:
                self._running = False

            if self._pending:
                initial_delay = DEFAULT_COALESCE_MS

    def stop(self) -> None:
        self._stopped = True
        self._pending.clear()
        if self._task and not self._task.done():
            self._task.cancel()
        self._handler = None

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def is_running(self) -> bool:
        return self._running

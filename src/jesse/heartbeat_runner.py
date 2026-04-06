"""LLM-driven heartbeat runner — openclaw philosophy.

Replaces the programmatic HeartbeatEngine monitor loop with an
agent-turn-based runner that:
- Runs full LLM turns with configurable prompts
- Reads HEARTBEAT.md workspace file if present
- Deduplicates repeated messages (24h window)
- Delivers alerts to Telegram (or suppresses per visibility config)
- Prunes HEARTBEAT_OK responses from conversation history
- Supports isolated sessions and lightweight context
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jesse.heartbeat_config import (
    HeartbeatConfig,
    is_within_active_hours,
    resolve_heartbeat_config,
)
from jesse.wake_layer import DEFAULT_COALESCE_MS, WakeLayer

logger = logging.getLogger(__name__)

HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"
HEARTBEAT_FILE = Path.home() / ".mt5-mcp" / "HEARTBEAT.md"
DEDUP_WINDOW_S = 86_400
STATE_FILE = Path.home() / ".mt5-mcp" / "heartbeat_state.json"


@dataclass(frozen=True)
class HeartbeatResult:
    status: str
    reason: str
    preview: str | None = None
    duration_ms: float | None = None


class HeartbeatRunner:
    def __init__(
        self,
        agent,
        mcp_client,
        telegram_bot=None,
        config: HeartbeatConfig | None = None,
        context_builder=None,
    ) -> None:
        self._agent = agent
        self._mcp_client = mcp_client
        self._telegram_bot = telegram_bot
        self._config = config or resolve_heartbeat_config()
        self._context_builder = context_builder
        self._last_payload_hash: str | None = None
        self._last_payload_at: float = 0.0
        self._wake_layer = WakeLayer()
        self._interval_task: asyncio.Task | None = None
        self._running = False

    def start(self) -> asyncio.Task:
        if self._running:
            return self._interval_task  # type: ignore[return-value]
        self._running = True
        self._wake_layer.set_handler(handler=self._wake_handler)
        self._wake_layer.request_wake(reason="interval", coalesce_ms=0)
        self._interval_task = self._wake_layer._task  # type: ignore[assignment]
        logger.info("HeartbeatRunner started (every=%s)", self._config.every)
        return self._interval_task  # type: ignore[return-value]

    def stop(self) -> None:
        self._running = False
        self._wake_layer.stop()
        self._interval_task = None
        logger.info("HeartbeatRunner stopped")

    async def run_once(self, reason: str = "interval") -> HeartbeatResult:
        return await self._execute(reason)

    async def _wake_handler(self, reason: str) -> tuple[bool, str]:
        try:
            result = await self._execute(reason)
            if result.status == "failed":
                logger.warning("Heartbeat failed: %s", result.reason)
                return (False, result.reason)
            if result.status == "sent":
                logger.info("Heartbeat sent: %s", result.preview or "")
        except Exception:
            logger.exception("Heartbeat wake handler error")
            return (False, "handler-exception")

        if self._running:
            self._wake_layer.request_wake(
                reason="interval", coalesce_ms=DEFAULT_COALESCE_MS
            )

        return (True, "")

    async def _execute(self, reason: str) -> HeartbeatResult:
        cfg = self._config

        if not cfg.enabled:
            return HeartbeatResult("skipped", "disabled")

        if not is_within_active_hours(cfg):
            return HeartbeatResult("skipped", "outside-active-hours")

        heartbeat_file_content = self._read_heartbeat_file()
        if heartbeat_file_content is not None and not heartbeat_file_content.strip():
            return HeartbeatResult("skipped", "empty-heartbeat-file")

        market_context = ""
        if self._context_builder and not cfg.light_context:
            market_context = self._context_builder()

        prompt = self._build_prompt(cfg, heartbeat_file_content, market_context, reason)

        t0 = time.monotonic()
        try:
            response = await self._agent.run_autonomous_cycle(
                context={"heartbeat_mode": True, "market_context": market_context}
            )
        except Exception:
            logger.exception("Heartbeat LLM call failed")
            return HeartbeatResult("failed", "llm-error")

        elapsed_ms = (time.monotonic() - t0) * 1000
        text = response.get("text", "").strip()

        if self._is_heartbeat_ok(text):
            self._prune_if_ok(text)
            if not cfg.show_ok:
                return HeartbeatResult("ok", "heartbeat-ok", duration_ms=elapsed_ms)

        payload = self._strip_ok_token(text)
        if not payload or len(payload) <= cfg.ack_max_chars:
            return HeartbeatResult("ok", "heartbeat-ok-empty", duration_ms=elapsed_ms)

        if self._is_duplicate(payload):
            return HeartbeatResult("skipped", "deduplicated", preview=payload[:80])

        self._record_payload(payload)

        if cfg.show_alerts:
            await self._deliver(payload)

        return HeartbeatResult(
            "sent", reason, preview=payload[:200], duration_ms=elapsed_ms
        )

    def _read_heartbeat_file(self) -> str | None:
        if HEARTBEAT_FILE.exists():
            return HEARTBEAT_FILE.read_text()
        return None

    def _build_prompt(
        self,
        cfg: HeartbeatConfig,
        file_content: str | None,
        market_context: str,
        reason: str,
    ) -> str:
        parts = [cfg.effective_prompt]

        if file_content:
            parts.append(f"\n\nHEARTBEAT.md:\n{file_content}")

        if market_context:
            parts.append(f"\n\nMARKET CONTEXT:\n{market_context}")

        parts.append(f"\n\nWake reason: {reason}")
        return "\n".join(parts)

    def _payload_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _is_duplicate(self, payload: str) -> bool:
        h = self._payload_hash(payload)
        if h == self._last_payload_hash:
            return (time.monotonic() - self._last_payload_at) < DEDUP_WINDOW_S
        return False

    def _record_payload(self, payload: str) -> None:
        self._last_payload_hash = self._payload_hash(payload)
        self._last_payload_at = time.monotonic()

    async def _deliver(self, text: str) -> None:
        if self._telegram_bot and self._config.target in ("telegram", "last"):
            try:
                await self._telegram_bot.send(text, parse_mode="Text")
                logger.info("Heartbeat delivered to Telegram: %s", text[:80])
            except Exception:
                logger.exception("Heartbeat delivery failed")
        else:
            logger.info("Heartbeat (no delivery target): %s", text[:120])

    @staticmethod
    def _is_heartbeat_ok(text: str) -> bool:
        upper = text.upper()
        return upper.startswith(HEARTBEAT_OK_TOKEN) or upper.endswith(
            HEARTBEAT_OK_TOKEN
        )

    @staticmethod
    def _strip_ok_token(text: str) -> str:
        upper = text.upper()
        if upper.startswith(HEARTBEAT_OK_TOKEN):
            text = text[len(HEARTBEAT_OK_TOKEN) :].strip()
        if upper.endswith(HEARTBEAT_OK_TOKEN):
            text = text[: -len(HEARTBEAT_OK_TOKEN)].strip()
        return text

    def _prune_if_ok(self, text: str) -> None:
        pass

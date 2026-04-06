"""Tests for HeartbeatRunner — LLM-driven heartbeat with WakeLayer."""

import asyncio
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from jesse.heartbeat_runner import (
    HeartbeatRunner,
    HeartbeatResult,
    HEARTBEAT_OK_TOKEN,
)
from jesse.heartbeat_config import HeartbeatConfig


def make_runner(**overrides):
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "all clear"})
    mcp = MagicMock()
    telegram = MagicMock()
    telegram.send = AsyncMock()
    cfg = HeartbeatConfig(**overrides)
    return HeartbeatRunner(
        agent=agent, mcp_client=mcp, telegram_bot=telegram, config=cfg
    )


@pytest.mark.asyncio
async def test_run_once_calls_agent():
    runner = make_runner()
    result = await runner.run_once("test")
    runner._agent.run_autonomous_cycle.assert_called_once()
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_disabled_skips():
    runner = make_runner(enabled=False)
    result = await runner.run_once("test")
    assert result.status == "skipped"
    assert result.reason == "disabled"
    runner._agent.run_autonomous_cycle.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_ok_shown_when_configured():
    runner = make_runner(
        show_ok=True, show_alerts=True, target="telegram", ack_max_chars=200
    )
    runner._agent.run_autonomous_cycle = AsyncMock(
        return_value={"text": f"{HEARTBEAT_OK_TOKEN} " + "A" * 400}
    )
    result = await runner.run_once("test")
    assert result.status == "sent"
    runner._telegram_bot.send.assert_called_once()


@pytest.mark.asyncio
async def test_deduplication():
    runner = make_runner(
        show_ok=False, show_alerts=True, target="telegram", ack_max_chars=100
    )
    runner._agent.run_autonomous_cycle = AsyncMock(
        return_value={
            "text": "BUY XAUUSD at 2650 — strong momentum breakout above key resistance level with increasing volume and RSI divergence confirming the move"
        }
    )
    r1 = await runner.run_once("test")
    assert r1.status == "sent"
    r2 = await runner.run_once("test")
    assert r2.status == "skipped"
    assert r2.reason == "deduplicated"
    runner._telegram_bot.send.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_delivery():
    runner = make_runner(
        show_ok=False, show_alerts=True, target="telegram", ack_max_chars=100
    )
    runner._agent.run_autonomous_cycle = AsyncMock(
        return_value={
            "text": "SELL EURUSD now — support broken at 1.0850 with strong momentum, RSI divergence confirmed, expect further downside to 1.0800 level within next 2 hours based on current order flow analysis"
        }
    )
    await runner.run_once("test")
    runner._telegram_bot.send.assert_called_once()
    call_args = runner._telegram_bot.send.call_args
    assert "SELL EURUSD" in call_args[0][0]


@pytest.mark.asyncio
async def test_wake_layer_reschedules():
    runner = make_runner(every="60s")
    runner.start()
    assert runner._wake_layer is not None
    runner.stop()
    assert runner._wake_layer._stopped is True


@pytest.mark.asyncio
async def test_non_telegram_target_no_delivery():
    runner = make_runner(show_ok=False, show_alerts=True, target="none")
    runner._agent.run_autonomous_cycle = AsyncMock(
        return_value={"text": "SELL EURUSD now"}
    )
    await runner.run_once("test")
    runner._telegram_bot.send.assert_not_called()


@pytest.mark.asyncio
async def test_large_payload_truncated():
    runner = make_runner(show_ok=False, show_alerts=True, target="telegram")
    runner._agent.run_autonomous_cycle = AsyncMock(return_value={"text": "X" * 500})
    result = await runner.run_once("test")
    assert result.status == "sent"
    assert result.preview is not None
    assert len(result.preview) <= 200

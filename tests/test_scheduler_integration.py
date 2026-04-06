"""Integration tests for AgentScheduler — full bootstrap flow."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from jesse.scheduler import (
    AgentScheduler,
    get_active_symbols,
    WEEKDAY_SYMBOLS,
    WEEKEND_SYMBOLS,
)


def test_get_active_symbols_weekday():
    import datetime

    monday = datetime.datetime(2025, 4, 7, 10, 0, tzinfo=datetime.timezone.utc)
    with patch("jesse.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = monday
        mock_dt.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
        symbols = get_active_symbols()
        assert symbols == WEEKDAY_SYMBOLS


def test_compute_interval_traded():
    result = {"text": "Traded BTCUSD at 85000"}
    interval, reason = AgentScheduler._compute_interval(result)
    assert interval == 5
    assert "Active trade" in reason


def test_compute_interval_hold():
    result = {"text": "Hold — no clear edge"}
    interval, reason = AgentScheduler._compute_interval(result)
    assert interval == 15
    assert "standard" in reason.lower() or "No clear" in reason


def test_compute_interval_circuit_breaker():
    result = {"text": "Circuit breaker triggered, cool off"}
    interval, reason = AgentScheduler._compute_interval(result)
    assert interval == 120
    assert "Cool-off" in reason


def test_compute_interval_default():
    result = {"text": "Nothing interesting happening"}
    interval, reason = AgentScheduler._compute_interval(result)
    assert interval == 15


@pytest.mark.asyncio
async def test_trigger_cycle():
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(
        return_value={"text": "No clear edge, holding"}
    )
    mcp = MagicMock()
    scheduler = AgentScheduler(jesse_agent=agent, mcp_client=mcp)
    result = await scheduler.trigger_cycle()
    assert result is not None
    assert scheduler.current_interval == 15


@pytest.mark.asyncio
async def test_trigger_cycle_trade():
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(
        return_value={"text": "Executed BUY on XAUUSD"}
    )
    mcp = MagicMock()
    scheduler = AgentScheduler(jesse_agent=agent, mcp_client=mcp)
    await scheduler.trigger_cycle()
    assert scheduler.current_interval == 5


@pytest.mark.asyncio
async def test_init_heartbeat():
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "ok"})
    mcp = MagicMock()
    scheduler = AgentScheduler(jesse_agent=agent, mcp_client=mcp)
    scheduler.init_heartbeat(symbols=["EURUSD", "XAUUSD"])
    assert scheduler.heartbeat_runner is not None
    assert scheduler.monitor_manager is not None


@pytest.mark.asyncio
async def test_init_cron():
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "ok"})
    mcp = MagicMock()
    scheduler = AgentScheduler(jesse_agent=agent, mcp_client=mcp)
    scheduler.init_cron()
    assert scheduler.cron_service is not None


@pytest.mark.asyncio
async def test_full_bootstrap_flow():
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "all clear"})
    mcp = MagicMock()
    telegram = MagicMock()
    telegram.send = AsyncMock()

    with tempfile.TemporaryDirectory() as tmpdir:
        from jesse.cron_config import CronConfig
        from jesse.heartbeat_config import HeartbeatConfig
        from jesse.scheduler import AgentScheduler

        sched = AgentScheduler(
            jesse_agent=agent,
            mcp_client=mcp,
            telegram_bot=telegram,
        )

        # Init heartbeat with limited symbols
        sched.init_heartbeat(symbols=["EURUSD"])
        assert sched.heartbeat_runner is not None
        assert sched.monitor_manager is not None

        # Init cron
        sched.init_cron()
        assert sched.cron_service is not None

        # Start heartbeat (non-blocking, uses WakeLayer)
        await sched.start_heartbeat()
        assert sched.heartbeat_runner._running

        # Start cron
        await sched.start_cron()
        assert sched.cron_service._running

        # Verify builtin cron jobs were added
        jobs = sched.cron_service.list_jobs()
        assert len(jobs) >= 4

        job_names = [j.name for j in jobs]
        assert "pre_market_scan" in job_names
        assert "weekend_crypto_scan" in job_names
        assert "eod_review" in job_names
        assert "memory_consolidation" in job_names

        # Run a heartbeat cycle
        result = await sched.heartbeat_runner.run_once("integration_test")
        assert result.status in ("ok", "sent", "skipped")

        # Shutdown
        sched.shutdown()
        assert not sched.heartbeat_runner._running
        assert not sched.cron_service._running


@pytest.mark.asyncio
async def test_scheduler_shutdown_order():
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "ok"})
    mcp = MagicMock()
    sched = AgentScheduler(jesse_agent=agent, mcp_client=mcp)
    sched.init_heartbeat(symbols=["EURUSD"])
    sched.init_cron()
    await sched.start_heartbeat()
    await sched.start_cron()
    sched.shutdown()
    assert not sched._running

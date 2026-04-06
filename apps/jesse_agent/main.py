"""Autonomous 24/7 AI Trading Agent — Main Entry Point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("jesse_agent")

_STATE_DIR = Path.home() / ".mt5-mcp"
_SHUTDOWN_TS = _STATE_DIR / "shutdown-timestamp.json"


def _record_shutdown(reason: str = "graceful"):
    """Record shutdown timestamp for restart detection on next boot."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _SHUTDOWN_TS.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                }
            )
        )
        logger.info("Shutdown timestamp recorded: %s", reason)
    except Exception as exc:
        logger.warning("Failed to record shutdown timestamp: %s", exc)


def _read_startup_info() -> dict[str, Any]:
    """Read shutdown timestamp and calculate downtime. Returns dict with restart info."""
    info: dict[str, Any] = {"is_restart": False, "downtime_seconds": 0, "reason": None}
    if not _SHUTDOWN_TS.exists():
        return info
    try:
        data = json.loads(_SHUTDOWN_TS.read_text())
        shutdown_at = datetime.fromisoformat(data["timestamp"])
        now = datetime.now(timezone.utc)
        downtime = (now - shutdown_at).total_seconds()
        info["is_restart"] = True
        info["downtime_seconds"] = int(downtime)
        info["reason"] = data.get("reason")
        return info
    except Exception as exc:
        logger.warning("Failed to read startup info: %s", exc)
        return info


def _format_downtime(seconds: int) -> str:
    """Format downtime in human-readable form."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m"


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def _start_health_server():
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    import uvicorn

    from .health import app as health_app

    config = uvicorn.Config(
        health_app, host="127.0.0.1", port=8090, log_level="warning"
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)
    logger.info("Health endpoint listening on :8090")


async def preflight() -> None:
    """Run comprehensive dependency checks before agent boot.

    Checks all critical and optional dependencies in order.
    Exits with code 1 if any critical check fails.
    Total execution time: < 15 seconds.
    """
    logger.info("Running preflight checks...")
    all_ok = True

    # 1. MCP Server (critical)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("http://127.0.0.1:8010/health")
            if resp.status_code == 200:
                logger.info("  [OK] MCP server healthy (port 8010)")
            else:
                logger.critical(
                    "  [FAIL] MCP server returned HTTP %d (expected 200)",
                    resp.status_code,
                )
                all_ok = False
    except Exception as exc:
        logger.critical(
            "  [FAIL] MCP server unreachable at http://127.0.0.1:8010/health: %s",
            exc,
        )
        all_ok = False

    # 2. TCP Bridge (non-critical — HTTP fallback available)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://127.0.0.1:8025/status")
            resp.json()  # Validate JSON response
            logger.info("  [OK] TCP bridge responding (port 8025)")
    except Exception as exc:
        logger.warning(
            "  [WARN] TCP bridge unavailable at http://127.0.0.1:8025/status: %s "
            "(falling back to HTTP gateway)",
            exc,
        )

    # 3. HTTP Gateway (critical)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("http://127.0.0.1:8020/bridge/terminal/status")
            if resp.status_code == 200:
                logger.info("  [OK] HTTP gateway healthy (port 8020)")
            else:
                logger.critical(
                    "  [FAIL] HTTP gateway returned HTTP %d (expected 200)",
                    resp.status_code,
                )
                all_ok = False
    except Exception as exc:
        logger.critical(
            "  [FAIL] HTTP gateway unreachable at http://127.0.0.1:8020/bridge/terminal/status: %s",
            exc,
        )
        all_ok = False

    # 4. Data directory
    data_dir = Path.home() / ".mt5-mcp"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Test writability
        test_file = data_dir / ".preflight_write_test"
        test_file.touch()
        test_file.unlink()
        logger.info("  [OK] Data directory writable: %s", data_dir)
    except Exception as exc:
        logger.critical("  [FAIL] Data directory not writable: %s: %s", data_dir, exc)
        all_ok = False

    # 5. Environment variables
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        logger.warning(
            "  [WARN] TELEGRAM_BOT_TOKEN not set — Telegram bot will be disabled"
        )
    else:
        logger.info("  [OK] TELEGRAM_BOT_TOKEN is set")

    if not os.environ.get("TELEGRAM_CHAT_ID"):
        logger.warning(
            "  [WARN] TELEGRAM_CHAT_ID not set — Telegram bot will be disabled"
        )
    else:
        logger.info("  [OK] TELEGRAM_CHAT_ID is set")

    # 6. Port availability (port 8090 for health server)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        result = sock.connect_ex(("127.0.0.1", 8090))
        if result == 0:
            logger.critical(
                "  [FAIL] Port 8090 is already in use — another instance may be running"
            )
            all_ok = False
        else:
            logger.info("  [OK] Port 8090 is available")
    finally:
        sock.close()

    if not all_ok:
        logger.critical("Preflight checks FAILED — aborting startup")
        sys.exit(1)

    logger.info("Preflight checks passed")


async def main():
    _load_dotenv()
    from jesse.react_agent import create_agent_resources
    from jesse.conversation import ConversationManager
    from jesse.scheduler import AgentScheduler
    from jesse.circuit_breaker import CircuitBreaker
    from .health import update_health

    Path.home().joinpath(".mt5-mcp").mkdir(parents=True, exist_ok=True)

    await preflight()

    startup_info = _read_startup_info()
    if startup_info["is_restart"]:
        logger.info(
            "Restart detected — downtime: %s (reason: %s)",
            _format_downtime(startup_info["downtime_seconds"]),
            startup_info["reason"],
        )
    else:
        logger.info("Cold start — no previous shutdown timestamp found")

    await _start_health_server()

    resources = await create_agent_resources()

    try:
        health = await resources.mcp_client.health_check()
        logger.info("MCP server healthy: %s", health)
    except Exception as exc:
        logger.error("Cannot reach MCP server: %s", exc)
        update_health(phase="ERROR")
        await resources.close()
        sys.exit(1)

    conversation_mgr = ConversationManager()

    try:
        acct = await asyncio.wait_for(
            resources.mcp_client.account_summary(), timeout=10.0
        )
        equity = acct.get("equity", 200.0)
        logger.info("Account: equity=$%.2f", equity)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("Could not fetch account summary: %s", exc)
        equity = 200.0

    circuit_breaker = CircuitBreaker(equity=equity)

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat = os.environ.get("TELEGRAM_CHAT_ID")
    bot = None
    if telegram_token and telegram_chat:
        from jesse.telegram_bot import TelegramBot

        bot = TelegramBot(
            bot_token=telegram_token,
            chat_id=telegram_chat,
            scheduler=None,
            mcp_client=resources.mcp_client,
            circuit_breaker=circuit_breaker,
            jesse_agent=resources.agent,
            conversation_mgr=conversation_mgr,
            startup_info=startup_info,
            equity=equity,
        )
        logger.info("Telegram bot configured")
    else:
        logger.warning(
            "Telegram bot not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)"
        )

    scheduler = AgentScheduler(
        jesse_agent=resources.agent,
        mcp_client=resources.mcp_client,
        circuit_breaker=circuit_breaker,
        telegram_bot=bot,
    )

    def _build_heartbeat_context() -> str:
        parts = []
        try:
            from jesse.scheduler import get_active_symbols

            parts.append(f"Active symbols: {', '.join(get_active_symbols())}")
        except Exception:
            pass
        ok, reason = circuit_breaker.check_all()
        if not ok:
            parts.append(f"CIRCUIT BREAKER: {reason}")
        return "\n".join(parts) if parts else ""

    scheduler.init_heartbeat(context_builder=_build_heartbeat_context)
    scheduler.init_cron()

    if bot:
        bot.scheduler = scheduler
        bot.mcp_client = resources.mcp_client
        bot.jesse_agent = resources.agent

    # Orchestrator for agent-routed user commands (with MCP fallback)
    from jesse.orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator(
        scheduler=scheduler,
        mcp_client=resources.mcp_client,
        circuit_breaker=circuit_breaker,
        jesse_agent=resources.agent,
    )

    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()
    shutting_down = False

    def handle_signal(sig):
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        logger.info("Signal %s received, shutting down...", sig)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    logger.info("Autonomous Trading Agent starting")
    from jesse.scheduler import get_active_symbols

    logger.info("Active symbols: %s", get_active_symbols())

    update_health(phase="STARTED")

    scheduler.start()
    await scheduler.start_heartbeat()
    await scheduler.start_cron()
    await orchestrator.start()

    bot_task = None
    if bot:
        bot_task = asyncio.create_task(bot.start())

    await shutdown_event.wait()

    update_health(phase="SHUTTING_DOWN")
    logger.info("Sending offline notification...")
    if bot:
        try:
            await bot.send("⚠️ Jesse going offline. I'll resume trading when I restart.")
        except Exception:
            pass

    scheduler.shutdown()
    await orchestrator.stop()

    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    if bot:
        await bot.stop()

    await resources.close()
    update_health(phase="STOPPED")
    _record_shutdown(reason="signal")
    logger.info("Agent shut down complete")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(main())

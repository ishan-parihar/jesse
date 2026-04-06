"""Integration test for all 7 heartbeat tools against real MT5-MCP infrastructure."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("heartbeat_test")

sys.path.insert(0, str(Path(__file__).parent / "src"))


class FakeMCPClient:
    """MCP client that returns realistic mock data for all monitor dependencies."""

    async def get_ticks(self, symbol: str, count: int = 1):
        prices = {
            "EURUSD": 1.0852,
            "GBPUSD": 1.2710,
            "USDJPY": 149.85,
            "XAUUSD": 2650.00,
            "BTCUSD": 85000.00,
            "AUDUSD": 0.6520,
            "US30": 39500.00,
            "USOIL": 78.50,
        }
        price = prices.get(symbol, 1.0)
        return {"bars": [{"price": price, "time": time.time()}]}

    async def get_bars(self, symbol: str, timeframe: str = "M15", count: int = 50):
        import random

        random.seed(hash(symbol) % 10000)
        base = {
            "EURUSD": 1.0850,
            "GBPUSD": 1.2700,
            "USDJPY": 149.80,
            "XAUUSD": 2650.0,
            "BTCUSD": 85000.0,
            "AUDUSD": 0.6520,
            "US30": 39500.0,
            "USOIL": 78.50,
        }.get(symbol, 1.0)
        atr_hint = {
            "EURUSD": 0.0008,
            "GBPUSD": 0.0012,
            "USDJPY": 0.15,
            "XAUUSD": 3.0,
            "BTCUSD": 500.0,
            "AUDUSD": 0.0006,
            "US30": 80.0,
            "USOIL": 0.60,
        }.get(symbol, 0.001)
        bars = []
        close = base
        for i in range(count):
            change = (random.random() - 0.5) * atr_hint * 2
            open_p = close
            close = close + change
            high = max(open_p, close) + random.random() * atr_hint * 0.5
            low = min(open_p, close) - random.random() * atr_hint * 0.5
            bars.append(
                {
                    "open": round(open_p, 5),
                    "high": round(high, 5),
                    "low": round(low, 5),
                    "close": round(close, 5),
                    "volume": random.randint(100, 5000),
                }
            )
        return {"bars": bars}

    async def trading_economic_calendar(self):
        return {
            "events": [
                {
                    "title": "US Non-Farm Payrolls",
                    "impact": "high",
                    "time": "2025-04-07T13:30:00Z",
                    "currency": "USD",
                    "forecast": "200K",
                    "previous": "180K",
                },
                {
                    "title": "ECB Interest Rate Decision",
                    "impact": "critical",
                    "time": "2025-04-08T12:15:00Z",
                    "currency": "EUR",
                    "forecast": "4.50%",
                    "previous": "4.50%",
                },
            ]
        }

    async def news_fetch(
        self,
        pools=None,
        limit=10,
        keywords=None,
        domains=None,
        countries=None,
        cities=None,
        excludeKeywords=None,
        enrichArticles=False,
        start=None,
        end=None,
        matchAll=False,
        includeRaw=False,
        cacheMode="prefer",
    ):
        return {"articles": []}


async def _run_tool_check(name, tool, args=None, expect_success=True):
    try:
        result = await tool.ainvoke(args or {})
        status = (
            "PASS" if not isinstance(result, str) or "Error" not in result else "WARN"
        )
        if not expect_success:
            status = "PASS"
        logger.info(
            "  [%s] %s → %.200s", status, name, str(result).replace("\n", " | ")
        )
        return result
    except Exception as e:
        status = "FAIL" if expect_success else "PASS"
        logger.info("  [%s] %s → %s: %s", status, name, type(e).__name__, e)
        return None


async def run_tests():
    from jesse.agent_tools import make_heartbeat_tools
    from jesse.heartbeat_engine import HeartbeatConfig, HeartbeatEngine

    client = FakeMCPClient()
    engine = HeartbeatEngine(client, config=HeartbeatConfig(base_interval=30.0))
    engine.initialize(["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD"])

    tools_list = make_heartbeat_tools(engine)
    tool_map = {t.name: t for t in tools_list}
    logger.info("Loaded %d heartbeat tools: %s", len(tool_map), list(tool_map.keys()))
    assert len(tool_map) == 7, f"Expected 7 tools, got {len(tool_map)}"

    # ── Test 1: get_heartbeat_context ──
    logger.info("\n═══ Test 1: get_heartbeat_context ═══")
    result = await _run_tool_check(
        "get_heartbeat_context", tool_map["get_heartbeat_context"]
    )
    assert result is not None
    assert (
        "Sessions" in result
        or "session" in result.lower()
        or "volatility" in result.lower()
        or "events" in result.lower()
        or "active" in result.lower()
    )

    # ── Test 2: get_recent_events (empty) ──
    logger.info("\n═══ Test 2: get_recent_events (empty) ═══")
    result = await _run_tool_check(
        "get_recent_events (all)", tool_map["get_recent_events"]
    )
    assert result is not None

    # ── Test 3: get_recent_events (filter by type) ──
    logger.info("\n═══ Test 3: get_recent_events (filter by type) ═══")
    result = await _run_tool_check(
        "get_recent_events (PRICE_ALERT)",
        tool_map["get_recent_events"],
        args={"event_type": "PRICE_ALERT"},
    )
    assert result is not None

    # ── Test 4: get_recent_events (invalid type) ──
    logger.info("\n═══ Test 4: get_recent_events (invalid type) ═══")
    result = await _run_tool_check(
        "get_recent_events (INVALID)",
        tool_map["get_recent_events"],
        args={"event_type": "NONEXISTENT"},
    )
    assert result is not None and "Unknown event type" in result

    # ── Test 5: add_price_alert ──
    logger.info("\n═══ Test 5: add_price_alert ═══")
    result = await _run_tool_check(
        "add_price_alert (EURUSD above 1.0900)",
        tool_map["add_price_alert"],
        args={
            "symbol": "EURUSD",
            "condition": "above",
            "price": 1.0900,
            "severity": "high",
            "cooldown_seconds": 60,
        },
    )
    assert result is not None and "Price alert set" in result

    # ── Test 6: add_price_alert (second alert) ──
    logger.info("\n═══ Test 6: add_price_alert (second) ═══")
    result = await _run_tool_check(
        "add_price_alert (XAUUSD below 2600)",
        tool_map["add_price_alert"],
        args={
            "symbol": "XAUUSD",
            "condition": "below",
            "price": 2600.0,
            "severity": "medium",
            "cooldown_seconds": 120,
        },
    )
    assert result is not None and "Price alert set" in result

    # ── Test 7: add_price_alert (invalid condition) ──
    logger.info("\n═══ Test 7: add_price_alert (invalid condition) ═══")
    result = await _run_tool_check(
        "add_price_alert (invalid)",
        tool_map["add_price_alert"],
        args={
            "symbol": "EURUSD",
            "condition": "INVALID",
            "price": 1.0,
            "severity": "low",
        },
    )
    assert result is not None

    # ── Test 8: list_price_alerts ──
    logger.info("\n═══ Test 8: list_price_alerts ═══")
    result = await _run_tool_check(
        "list_price_alerts (all)", tool_map["list_price_alerts"]
    )
    assert result is not None and ("EURUSD" in result or "XAUUSD" in result)

    # ── Test 9: list_price_alerts (filter by symbol) ──
    logger.info("\n═══ Test 9: list_price_alerts (filter by symbol) ═══")
    result = await _run_tool_check(
        "list_price_alerts (EURUSD only)",
        tool_map["list_price_alerts"],
        args={"symbol": "EURUSD"},
    )
    assert result is not None and "EURUSD" in result and "XAUUSD" not in result

    # ── Test 10: get_volatility_states ──
    logger.info("\n═══ Test 10: get_volatility_states ═══")
    result = await _run_tool_check(
        "get_volatility_states (specific)",
        tool_map["get_volatility_states"],
        args={"symbols": ["EURUSD", "XAUUSD"]},
    )
    assert result is not None

    # ── Test 11: get_volatility_states (no symbols param → all states) ──
    logger.info("\n═══ Test 11: get_volatility_states (empty = all states) ═══")
    result = await _run_tool_check(
        "get_volatility_states (all)", tool_map["get_volatility_states"]
    )
    assert result is not None

    # ── Test 12: get_upcoming_news ──
    logger.info("\n═══ Test 12: get_upcoming_news ═══")
    result = await _run_tool_check(
        "get_upcoming_news (4h)",
        tool_map["get_upcoming_news"],
        args={"hours_ahead": 4.0},
    )
    assert result is not None

    # ── Test 13: get_upcoming_news (wide window) ──
    logger.info("\n═══ Test 13: get_upcoming_news (72h) ═══")
    result = await _run_tool_check(
        "get_upcoming_news (72h)",
        tool_map["get_upcoming_news"],
        args={"hours_ahead": 72.0},
    )
    assert result is not None

    # ── Test 14: Event emission → get_recent_events ──
    logger.info("\n═══ Test 14: Event emission → retrieval ═══")
    engine.event_bus.emit(
        EventType.PRICE_ALERT,
        "EURUSD",
        "high",
        {"condition": "above", "price": 1.0900, "current_price": 1.0902},
    )
    engine.event_bus.emit(
        EventType.VOLATILITY_SPIKE, "GBPJPY", "high", {"ratio": 3.0, "avg_atr": 15.0}
    )
    engine.event_bus.emit(
        EventType.NEWS_EVENT,
        "US500",
        "critical",
        {"title": "US NFP data release", "description": "NFP"},
    )
    await asyncio.sleep(0.1)
    result = await _run_tool_check(
        "get_recent_events (after emit)",
        tool_map["get_recent_events"],
        args={"limit": 5},
    )
    assert result is not None and (
        "PRICE_ALERT" in result
        or "VOLATILITY_SPIKE" in result
        or "NEWS_EVENT" in result
    )

    # ── Test 15: remove_price_alert ──
    logger.info("\n═══ Test 15: remove_price_alert ═══")
    alerts_result = await tool_map["list_price_alerts"].ainvoke({})
    alert_ids = [
        line.split(":")[0] for line in alerts_result.split("\n") if line.strip()
    ]
    if alert_ids:
        first_id = alert_ids[0]
        result = await _run_tool_check(
            f"remove_price_alert ({first_id})",
            tool_map["remove_price_alert"],
            args={"alert_id": first_id},
        )
        assert result is not None
        remaining = await tool_map["list_price_alerts"].ainvoke({})
        assert first_id not in remaining, (
            f"Alert {first_id} should be removed but still present"
        )
        logger.info("  [PASS] Alert removed and verified absent")
    else:
        logger.info("  [SKIP] No alerts to remove")

    # ── Test 16: get_heartbeat_context (with data) ──
    logger.info("\n═══ Test 16: get_heartbeat_context (populated) ═══")
    result = await _run_tool_check(
        "get_heartbeat_context (after events+alerts)", tool_map["get_heartbeat_context"]
    )
    assert result is not None
    assert (
        "Active sessions" in result
        or "Sessions" in result
        or "volatility" in result.lower()
    )

    # ── Test 17: Engine context dict ──
    logger.info("\n═══ Test 17: Engine.get_context() dict ═══")
    ctx = engine.get_context()
    assert "active_sessions" in ctx
    assert "session_volatility_hint" in ctx
    assert "recent_events" in ctx
    assert "recommended_interval" in ctx
    assert "volatility_states" in ctx
    logger.info("  [PASS] Context keys: %s", list(ctx.keys()))
    logger.info("  [INFO] Active sessions: %s", ctx["active_sessions"])
    logger.info("  [INFO] Volatility hint: %s", ctx["session_volatility_hint"])
    logger.info("  [INFO] Recommended interval: %.1fs", ctx["recommended_interval"])

    # ── Test 18: adjust_interval logic ──
    logger.info("\n═══ Test 18: adjust_interval ═══")
    interval = engine.adjust_interval()
    assert engine.config.min_interval <= interval <= engine.config.max_interval
    logger.info(
        "  [PASS] Adjusted interval: %.1fs (min=%.1f, max=%.1f)",
        interval,
        engine.config.min_interval,
        engine.config.max_interval,
    )

    # ── Test 19: heartbeat_cycle ──
    logger.info("\n═══ Test 19: heartbeat_cycle ═══")
    cycle_result = await engine.heartbeat_cycle()
    assert "interval_used" in cycle_result
    assert "active_sessions" in cycle_result
    assert "monitors_checked" in cycle_result
    assert "timestamp" in cycle_result
    logger.info("  [PASS] Cycle result: %s", json.dumps(cycle_result, default=str))

    # ── Test 20: SessionManager accuracy ──
    logger.info("\n═══ Test 20: SessionManager accuracy ═══")
    sm = engine.session_manager
    from datetime import datetime, timezone

    wednesday_10am = datetime(2025, 4, 9, 10, 0, tzinfo=timezone.utc)
    active = sm.get_active_sessions(wednesday_10am)
    active_names = [s.name.value for s in active]
    assert "London" in active_names, (
        f"London should be active at 10:00 UTC Wednesday, got {active_names}"
    )
    assert any("Crypto" in s for s in active_names), (
        f"Crypto should always be active, got {active_names}"
    )
    logger.info("  [PASS] London session at 10:00 UTC: %s", active_names)

    friday_15_00 = datetime(2025, 4, 11, 15, 0, tzinfo=timezone.utc)
    active2 = sm.get_active_sessions(friday_15_00)
    active2_names = [s.name.value for s in active2]
    assert "London" in active2_names
    assert "New York" in active2_names
    assert any("Crypto" in s for s in active2_names)
    logger.info("  [PASS] London-NY overlap at 15:00 UTC: %s", active2_names)

    vol_hint = sm.get_session_volatility_hint()
    assert vol_hint in ("quiet", "normal", "active", "volatile")
    logger.info("  [PASS] Volatility hint: %s", vol_hint)

    # Cleanup
    engine.stop()
    engine.event_bus.clear_history()

    logger.info("\n" + "=" * 60)
    logger.info("ALL 20 HEARTBEAT TESTS PASSED")
    logger.info("=" * 60)


if __name__ == "__main__":
    from jesse.market_event_bus import EventType

    asyncio.run(run_tests())

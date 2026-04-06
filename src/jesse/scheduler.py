"""Scheduler — wires HeartbeatRunner + CronService for Jesse's autonomous cycles.

Keeps the trigger_cycle agent execution logic from the old APScheduler version,
but replaces the scheduling layer with the new LLM-driven architecture.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from jesse.cron_service import CronSchedule, CronService
from jesse.heartbeat_config import resolve_heartbeat_config
from jesse.heartbeat_runner import HeartbeatRunner

logger = logging.getLogger(__name__)

WEEKDAY_SYMBOLS = [
    "EURUSD",
    "USDJPY",
    "GBPJPY",
    "AUDUSD",
    "US30",
    "XAUUSD",
    "USOIL",
    "BTCUSD",
]
WEEKEND_SYMBOLS = ["BTCUSD", "ETHUSD"]


def get_active_symbols() -> list[str]:
    now = datetime.now(timezone.utc)
    return WEEKDAY_SYMBOLS if now.weekday() < 5 else WEEKEND_SYMBOLS


_TRADE_RE = re.compile(
    r"\b(traded|bought|sold|executed|opened|entered)\b", re.IGNORECASE
)
_HOLD_RE = re.compile(r"\b(hold|no.clear.edge|waiting|monitoring)\b", re.IGNORECASE)
_BREAKER_RE = re.compile(
    r"\b(circuit.breaker|cool.off|stop.trading|daily.limit)\b", re.IGNORECASE
)


class AgentScheduler:
    def __init__(
        self, jesse_agent, mcp_client, circuit_breaker=None, telegram_bot=None
    ):
        self.jesse_agent = jesse_agent
        self.mcp_client = mcp_client
        self.circuit_breaker = circuit_breaker
        self.telegram_bot = telegram_bot
        self.current_interval = 15
        self._running = False

        self.heartbeat_runner: HeartbeatRunner | None = None
        self.cron_service: CronService | None = None
        self.monitor_manager = None
        self._monitor_tasks: list[asyncio.Task] = []

    async def trigger_cycle(self, context: dict | None = None) -> dict:
        if context is None:
            context = self._build_context()
        try:
            result = await self.jesse_agent.run_autonomous_cycle(context=context)
            interval_mins, reason = self._compute_interval(result)
            self.current_interval = interval_mins
            logger.info("Cycle complete — next interval: %s", reason)
            return result
        except Exception as exc:
            logger.error("Agent cycle failed: %s", exc, exc_info=True)
            self.current_interval = min(self.current_interval * 2, 60)
            raise

    def _build_context(self) -> dict:
        context = {}
        if self.circuit_breaker:
            ok, reason = self.circuit_breaker.check_all()
            context["circuit_breaker"] = (ok, reason)
            context["consecutive_losses"] = (
                self.circuit_breaker.state.consecutive_losses
            )
        context["active_symbols"] = get_active_symbols()
        return context

    @staticmethod
    def _compute_interval(result: dict) -> tuple[int, str]:
        text = result.get("text", "").lower()
        if _BREAKER_RE.search(text):
            return (120, "Cool-off period")
        if _TRADE_RE.search(text):
            return (5, "Active trade — tight monitoring")
        if _HOLD_RE.search(text):
            return (15, "No clear edge, standard interval")
        return (15, "Default interval")

    def init_heartbeat(self, symbols: list[str] | None = None, context_builder=None):
        from jesse.monitor_manager import MarketMonitorManager

        syms = symbols or get_active_symbols()
        self.monitor_manager = MarketMonitorManager(
            mcp_client=self.mcp_client, symbols=syms
        )

        def _full_context_builder() -> str:
            parts = []
            if context_builder:
                parts.append(context_builder())
            if self.monitor_manager:
                try:
                    vol_hint = self.monitor_manager.session_manager.get_session_volatility_hint()
                    parts.append(f"Market volatility: {vol_hint}")
                except Exception:
                    pass
                try:
                    states = self.monitor_manager.vol_monitor.get_all_states()
                    if states:
                        lines = [f"  {s}: {v}" for s, v in states.items() if v]
                        parts.append("Volatility states:\n" + "\n".join(lines))
                except Exception:
                    pass
                try:
                    events = self.monitor_manager.event_bus.get_recent_events(limit=5)
                    if events:
                        from jesse.heartbeat_engine import HeartbeatEngine

                        formatted = HeartbeatEngine._format_recent_events(events)
                        if formatted:
                            parts.append(
                                "Recent events:\n"
                                + "\n".join(f"  - {e}" for e in formatted)
                            )
                except Exception:
                    pass
            return "\n\n".join(p for p in parts if p)

        cfg = resolve_heartbeat_config()
        self.heartbeat_runner = HeartbeatRunner(
            agent=self.jesse_agent,
            mcp_client=self.mcp_client,
            telegram_bot=self.telegram_bot,
            config=cfg,
            context_builder=_full_context_builder,
        )
        logger.info(
            "HeartbeatRunner initialized (every=%s, monitors for %d symbols)",
            cfg.every,
            len(syms),
        )

    async def start_heartbeat(self):
        if self.monitor_manager:
            self.monitor_manager.start()
        if self.heartbeat_runner:
            self.heartbeat_runner.start()

    def init_cron(self):
        self.cron_service = CronService(
            agent=self.jesse_agent,
            mcp_client=self.mcp_client,
            telegram_bot=self.telegram_bot,
            heartbeat_runner=self.heartbeat_runner,
        )

    async def start_cron(self):
        if self.cron_service:
            await self.cron_service.start()
            await self._add_builtin_cron_jobs()

    async def _add_builtin_cron_jobs(self):
        if not self.cron_service:
            return

        await self.cron_service.add_job(
            name="pre_market_scan",
            schedule=CronSchedule.cron("45 7 * * 1-5", tz="Asia/Calcutta"),
            payload="Run pre-market scan. Check all active symbols for opportunities.",
            session_target="main",
            wake_mode="now",
        )
        await self.cron_service.add_job(
            name="weekend_crypto_scan",
            schedule=CronSchedule.cron("0 10 * * 6,0", tz="Asia/Calcutta"),
            payload="Weekend crypto scan. Check BTCUSD and ETHUSD for opportunities.",
            session_target="main",
            wake_mode="now",
        )
        await self.cron_service.add_job(
            name="eod_review",
            schedule=CronSchedule.cron("0 22 * * 1-5", tz="Asia/Calcutta"),
            payload="End-of-day review. Review today's trades, check insights, update memory.",
            session_target="main",
            wake_mode="now",
        )
        await self.cron_service.add_job(
            name="memory_consolidation",
            schedule=CronSchedule.cron("0 23 * * *", tz="Asia/Calcutta"),
            payload="Memory consolidation. Extract patterns from trade history, apply decay to stale patterns.",
            session_target="isolated",
            wake_mode="now",
        )

    async def pre_market_scan(self):
        logger.info("Pre-market scan: %s", get_active_symbols())
        await self.trigger_cycle()

    async def weekend_crypto_scan(self):
        logger.info("Weekend crypto scan: %s", WEEKEND_SYMBOLS)
        await self.trigger_cycle()

    async def eod_review(self):
        logger.info("End-of-day review")
        try:
            insights = await self.mcp_client.trading_insights(lookback_days=1)
            logger.info("Daily insights: %s", insights)
        except Exception as exc:
            logger.error("EOD review failed: %s", exc)

    async def _memory_consolidation(self):
        from jesse.consolidation import consolidate
        from jesse.decay import apply_decay
        from jesse.semantic_memory import SemanticMemory
        from apps.autonomous_agent.health import update_health

        logger.info("Memory consolidation")
        try:
            patterns = consolidate()
            logger.info("Extracted %d patterns", len(patterns))
        except Exception as exc:
            logger.error("Consolidation failed: %s", exc, exc_info=True)
        try:
            memory = SemanticMemory()
            decay_result = apply_decay(memory)
            if decay_result.get("pruned", 0) > 0:
                logger.info("Decay pruned %d stale patterns", decay_result["pruned"])
            update_health(memory_count=memory.count())
        except Exception as exc:
            logger.error("Decay failed: %s", exc, exc_info=True)

    def start(self):
        self._running = True
        logger.info(
            "Scheduler started (initial interval: %d min)", self.current_interval
        )

    def shutdown(self):
        if self.heartbeat_runner:
            self.heartbeat_runner.stop()
        if self.cron_service:
            self.cron_service.stop()
        if self.monitor_manager:
            self.monitor_manager.stop()
        self._running = False
        logger.info("Scheduler shut down")

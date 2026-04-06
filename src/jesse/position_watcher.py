"""
PositionWatcher — Real-time monitoring of open positions.

Detects SL/TP hits within 30 seconds (vs waiting for next agent cycle).
Automatically journals position outcomes when they close.
"""

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PositionWatcher:
    """
    Background task that polls open positions every 30 seconds.
    When a position disappears (SL/TP hit), immediately journals the outcome.
    """

    def __init__(
        self,
        mcp_client: Any,
        check_interval: float = 30.0,
        trade_monitor_callback: Optional[Any] = None,
    ):
        self._mcp_client = mcp_client
        self._check_interval = check_interval
        self._trade_monitor_callback = trade_monitor_callback
        self._known_positions: dict[str, dict] = {}  # position_id -> position_data
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the position watcher background task."""
        self._running = True
        self._task = asyncio.create_task(self._watch_loop(), name="position-watcher")
        logger.info("PositionWatcher started (interval=%.0fs)", self._check_interval)

    async def stop(self):
        """Stop the position watcher."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PositionWatcher stopped")

    async def _watch_loop(self):
        """Main watch loop — runs every _check_interval seconds."""
        # Initial snapshot
        try:
            positions = await self._mcp_client.positions_open()
            self._known_positions = {
                str(p.get("id") or p.get("position_id") or f"pos_{i}"): p
                for i, p in enumerate(positions)
                if positions
            }
            logger.info(
                "PositionWatcher: initial snapshot of %d positions",
                len(self._known_positions),
            )
        except Exception as exc:
            logger.warning("PositionWatcher: initial snapshot failed: %s", exc)

        while self._running:
            await asyncio.sleep(self._check_interval)
            try:
                await self._check_positions()
            except Exception as exc:
                logger.error("PositionWatcher: check failed: %s", exc)

    async def _check_positions(self):
        """Compare current positions vs known positions. Journal any closures."""
        try:
            current_positions = await self._mcp_client.positions_open()
        except Exception as exc:
            logger.warning("PositionWatcher: positions_open failed: %s", exc)
            return

        current_map = {}
        for p in current_positions or []:
            pos_id = str(p.get("id") or p.get("position_id") or "")
            if pos_id:
                current_map[pos_id] = p

        # Find positions that existed before but don't now (closed)
        closed_ids = set(self._known_positions.keys()) - set(current_map.keys())

        for pos_id in closed_ids:
            old_pos = self._known_positions[pos_id]
            symbol = old_pos.get("symbol", "UNKNOWN")
            side = old_pos.get("type", "UNKNOWN").lower()
            entry_price = old_pos.get("price_open")
            pnl = float(old_pos.get("profit", 0) or 0)

            logger.info(
                "PositionWatcher: detected closure %s %s (PnL=$%.2f)", symbol, side, pnl
            )

            # Journal the outcome
            try:
                outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
                await self._mcp_client.trading_log_decision(
                    symbol=symbol,
                    side=side,
                    action="close",
                    model_justification=f"Position closed by MT5 (SL/TP hit). Detected by real-time position watcher.",
                    outcome=outcome,
                    regime="mt5_initiated",
                    entry_price=entry_price,
                    pnl=pnl,
                )
            except Exception as exc:
                logger.error("PositionWatcher: failed to journal closure: %s", exc)

            # Notify via callback
            if self._trade_monitor_callback:
                try:
                    await self._trade_monitor_callback(
                        "position_closed",
                        position=old_pos,
                        detected_by="position_watcher",
                    )
                except Exception as exc:
                    logger.error("PositionWatcher: callback failed: %s", exc)

        # Find NEW positions (opened outside agent cycle)
        new_ids = set(current_map.keys()) - set(self._known_positions.keys())
        for pos_id in new_ids:
            new_pos = current_map[pos_id]
            symbol = new_pos.get("symbol", "UNKNOWN")
            side = new_pos.get("type", "UNKNOWN").lower()
            logger.info(
                "PositionWatcher: detected new position %s %s (ID: %s)",
                symbol,
                side,
                pos_id,
            )
            if self._trade_monitor_callback:
                try:
                    await self._trade_monitor_callback(
                        "position_opened",
                        position=new_pos,
                        detected_by="position_watcher",
                    )
                except Exception as exc:
                    logger.error("PositionWatcher: callback failed: %s", exc)

        # Update known positions
        self._known_positions = current_map

    def get_stats(self) -> dict:
        """Return watcher statistics."""
        return {
            "running": self._running,
            "known_positions": len(self._known_positions),
            "check_interval": self._check_interval,
        }

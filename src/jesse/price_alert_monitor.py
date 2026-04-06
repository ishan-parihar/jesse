"""PriceAlertMonitor — threshold-based price wakeups for Jesse's autonomous trading agent.

Monitors price levels and emits MarketEvent notifications when thresholds are crossed.
Supports cooldowns to prevent alert spam, multiple conditions (above/below/crosses),
and severity-based filtering. Python stdlib + asyncio only.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jesse.market_event_bus import EventType, MarketEvent, MarketEventBus

if TYPE_CHECKING:
    from jesse.mcp_client import MCPClient

logger = logging.getLogger(__name__)

# ── PriceAlert ───────────────────────────────────────────────────────────────


@dataclass
class PriceAlert:
    """A single price-level alert with cooldown and state tracking."""

    symbol: str
    condition: str
    price: float
    severity: str = "medium"
    active: bool = True
    triggered: bool = False
    cooldown_seconds: float = 300.0
    last_triggered: float = 0.0
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        valid_conditions = {"above", "below", "crosses_up", "crosses_down"}
        if self.condition not in valid_conditions:
            raise ValueError(
                f"Invalid condition '{self.condition}'. Must be one of: {valid_conditions}"
            )
        valid_severities = {"low", "medium", "high"}
        if self.severity not in valid_severities:
            raise ValueError(
                f"Invalid severity '{self.severity}'. Must be one of: {valid_severities}"
            )


# ── PriceAlertMonitor ────────────────────────────────────────────────────────


class PriceAlertMonitor:
    """Monitors price levels and emits events when thresholds are crossed.

    Usage:
        monitor = PriceAlertMonitor(event_bus, mcp_client)
        alert_id = monitor.add_alert("EURUSD", "above", 1.0850, severity="high")
        task = monitor.start_monitoring(check_interval=5.0)
        # ... later ...
        monitor.stop_monitoring()
    """

    def __init__(self, event_bus: MarketEventBus, mcp_client: "MCPClient") -> None:
        self._event_bus = event_bus
        self._mcp_client = mcp_client
        self._alerts: list[PriceAlert] = []
        self._lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None

    # ── Alert Management ─────────────────────────────────────────────────

    async def add_alert(
        self,
        symbol: str,
        condition: str,
        price: float,
        severity: str = "medium",
        cooldown_seconds: float = 300.0,
    ) -> str:
        """Add a new price alert. Returns the alert ID."""
        alert = PriceAlert(
            symbol=symbol,
            condition=condition,
            price=price,
            severity=severity,
            cooldown_seconds=cooldown_seconds,
        )
        async with self._lock:
            self._alerts.append(alert)
        logger.info(
            "Added price alert %s: %s %s %s (severity=%s, cooldown=%.0fs)",
            alert.id,
            symbol,
            condition,
            price,
            severity,
            cooldown_seconds,
        )
        return alert.id

    async def remove_alert(self, alert_id: str) -> bool:
        """Remove an alert by ID. Returns True if found and removed."""
        async with self._lock:
            for i, alert in enumerate(self._alerts):
                if alert.id == alert_id:
                    self._alerts.pop(i)
                    logger.info("Removed price alert %s", alert_id)
                    return True
        logger.warning("Alert %s not found for removal", alert_id)
        return False

    def list_alerts_sync(self, symbol: str | None = None) -> list[PriceAlert]:
        if symbol is None:
            return list(self._alerts)
        return [a for a in self._alerts if a.symbol == symbol]

    async def list_alerts(self, symbol: str | None = None) -> list[PriceAlert]:
        """List active alerts, optionally filtered by symbol."""
        async with self._lock:
            if symbol is None:
                return list(self._alerts)
            return [a for a in self._alerts if a.symbol == symbol]

    async def clear_alerts(self, symbol: str | None = None) -> int:
        """Clear alerts, optionally filtered by symbol. Returns number cleared."""
        async with self._lock:
            if symbol is None:
                count = len(self._alerts)
                self._alerts.clear()
                logger.info("Cleared all %d price alerts", count)
                return count
            before = len(self._alerts)
            self._alerts = [a for a in self._alerts if a.symbol != symbol]
            count = before - len(self._alerts)
            logger.info("Cleared %d price alerts for %s", count, symbol)
            return count

    # ── Core Checking ────────────────────────────────────────────────────

    async def check_alerts(self) -> list[PriceAlert]:
        """Check all active alerts against current prices. Returns triggered alerts."""
        triggered: list[PriceAlert] = []

        async with self._lock:
            alerts_to_check = [a for a in self._alerts if a.active]

        if not alerts_to_check:
            return triggered

        # Group by symbol to minimize API calls
        symbols = set(a.symbol for a in alerts_to_check)
        prices: dict[str, float | None] = {}

        for symbol in symbols:
            current_price = await self._get_current_price(symbol)
            prices[symbol] = current_price

        for alert in alerts_to_check:
            current_price = prices.get(alert.symbol)
            if current_price is None:
                logger.warning(
                    "Could not fetch price for %s, skipping alert %s",
                    alert.symbol,
                    alert.id,
                )
                continue

            if self._should_fire(alert, current_price):
                alert.triggered = True
                alert.last_triggered = time.time()
                triggered.append(alert)
                self._emit_alert_event(alert, current_price)
                logger.info(
                    "Price alert fired: %s %s %.5f (current=%.5f)",
                    alert.symbol,
                    alert.condition,
                    alert.price,
                    current_price,
                )

        return triggered

    async def _check_single_alert(self, alert: PriceAlert) -> bool:
        """Check a single alert against current price. Returns True if fired."""
        if not alert.active:
            return False

        current_price = await self._get_current_price(alert.symbol)
        if current_price is None:
            logger.warning(
                "Could not fetch price for %s, skipping alert %s",
                alert.symbol,
                alert.id,
            )
            return False

        if self._should_fire(alert, current_price):
            alert.triggered = True
            alert.last_triggered = time.time()
            self._emit_alert_event(alert, current_price)
            logger.info(
                "Price alert fired: %s %s %.5f (current=%.5f)",
                alert.symbol,
                alert.condition,
                alert.price,
                current_price,
            )
            return True
        return False

    def _should_fire(self, alert: PriceAlert, current_price: float) -> bool:
        """Evaluate condition vs price vs cooldown. Returns True if alert should fire."""
        # Check cooldown
        if alert.triggered:
            elapsed = time.time() - alert.last_triggered
            if elapsed < alert.cooldown_seconds:
                return False
            # Cooldown expired — reset triggered state so it can fire again
            alert.triggered = False

        condition = alert.condition
        threshold = alert.price

        if condition == "above":
            return current_price > threshold
        elif condition == "below":
            return current_price < threshold
        elif condition == "crosses_up":
            # Price was at or below threshold, now above
            return current_price > threshold
        elif condition == "crosses_down":
            # Price was at or above threshold, now below
            return current_price < threshold

        logger.error("Unknown condition '%s' for alert %s", condition, alert.id)
        return False

    # ── Monitoring Loop ─────────────────────────────────────────────────

    def start_monitoring(self, check_interval: float = 5.0) -> asyncio.Task:
        """Start the async monitoring loop. Returns the asyncio.Task."""
        if self._monitor_task is not None and not self._monitor_task.done():
            logger.warning("Monitoring already running")
            return self._monitor_task

        self._monitor_task = asyncio.create_task(
            self._monitor_loop(check_interval),
            name="price-alert-monitor",
        )
        logger.info("Started price alert monitoring (interval=%.1fs)", check_interval)
        return self._monitor_task

    def stop_monitoring(self) -> None:
        """Cancel the monitoring task."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            logger.info("Stopped price alert monitoring")
        self._monitor_task = None

    async def _monitor_loop(self, check_interval: float) -> None:
        """Main monitoring loop — runs until cancelled."""
        try:
            while True:
                await self.check_alerts()
                await asyncio.sleep(check_interval)
        except asyncio.CancelledError:
            logger.info("Price alert monitoring loop cancelled")
            raise
        except Exception:
            logger.exception("Price alert monitoring loop crashed")
            raise

    # ── Internal Helpers ─────────────────────────────────────────────────

    async def _get_current_price(self, symbol: str) -> float | None:
        """Fetch current price for a symbol via MCP client."""
        try:
            ticks = await self._mcp_client.get_ticks(symbol, count=1)
            if isinstance(ticks, list) and len(ticks) > 0:
                tick = ticks[0]
                if isinstance(tick, dict) and "price" in tick:
                    return float(tick["price"])
            elif isinstance(ticks, dict):
                # Some responses wrap in a dict
                if (
                    "ticks" in ticks
                    and isinstance(ticks["ticks"], list)
                    and len(ticks["ticks"]) > 0
                ):
                    return float(ticks["ticks"][0].get("price", 0))
                if "price" in ticks:
                    return float(ticks["price"])
            logger.warning("Unexpected tick format for %s: %s", symbol, ticks)
            return None
        except Exception:
            logger.exception("Failed to fetch price for %s", symbol)
            return None

    def _emit_alert_event(self, alert: PriceAlert, current_price: float) -> None:
        """Emit a MarketEvent for a triggered alert."""
        self._event_bus.emit(
            event_type=EventType.PRICE_ALERT,
            symbol=alert.symbol,
            severity=alert.severity,
            data={
                "condition": alert.condition,
                "price": alert.price,
                "current_price": current_price,
                "alert_id": alert.id,
            },
        )

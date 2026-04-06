"""VolatilityMonitor — ATR-based volatility detection for Jesse's autonomous trading agent.

Tracks ATR and bar ranges to detect volatility spikes and compression (squeeze) patterns.
Python stdlib + asyncio only. No numpy/pandas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from jesse.market_event_bus import EventType, MarketEventBus

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

_SPIKE_MULTIPLIER = 2.0  # ATR > avg * 2.0  → spike
_COMPRESS_MULTIPLIER = 0.4  # ATR < avg * 0.4  → compression (squeeze)


# ── VolatilityState ──────────────────────────────────────────────────────────


@dataclass
class VolatilityState:
    """Per-symbol volatility snapshot."""

    symbol: str
    atr_current: float = 0.0
    atr_average: float = 0.0
    atr_percentile: float = 0.0
    bar_range_avg: float = 0.0
    last_update: float = 0.0
    regime: str = "normal"  # "normal" | "spike" | "compress"


# ── VolatilityMonitor ────────────────────────────────────────────────────────


class VolatilityMonitor:
    """Monitor ATR-based volatility across symbols and emit events on regime changes.

    Spike detection:    ATR > average * {_SPIKE_MULTIPLIER}
    Compression detect: ATR < average * {_COMPRESS_MULTIPLIER}  (squeeze setup)
    """

    def __init__(
        self,
        event_bus: MarketEventBus,
        mcp_client,
        atr_period: int = 14,
        lookback_bars: int = 50,
    ) -> None:
        self._event_bus = event_bus
        self._mcp = mcp_client
        self._atr_period = atr_period
        self._lookback_bars = lookback_bars
        self._states: dict[str, VolatilityState] = {}
        self._monitor_task: asyncio.Task | None = None
        self._monitoring = False

    # ── State access ─────────────────────────────────────────────────────

    def get_state(self, symbol: str) -> VolatilityState | None:
        """Return current volatility state for *symbol*, or ``None``."""
        return self._states.get(symbol)

    def get_all_states(self) -> dict[str, VolatilityState]:
        """Return a snapshot of all tracked symbol states."""
        return dict(self._states)

    # ── State computation ────────────────────────────────────────────────

    async def update_state(self, symbol: str) -> VolatilityState:
        """Fetch latest bars, compute ATR and bar-range stats, store state.

        Returns the updated ``VolatilityState``.
        """
        bars_result = await self._mcp.get_bars(
            symbol, timeframe="M15", count=self._lookback_bars
        )

        bars = self._extract_bars(bars_result)
        if len(bars) < self._atr_period + 1:
            logger.warning(
                "Not enough bars for %s: got %d, need %d",
                symbol,
                len(bars),
                self._atr_period + 1,
            )
            if symbol not in self._states:
                self._states[symbol] = VolatilityState(symbol=symbol)
            return self._states[symbol]

        atr_values = self._compute_atr_series(bars, self._atr_period)

        current_atr = atr_values[-1] if atr_values else 0.0
        atr_avg = sum(atr_values) / len(atr_values) if atr_values else 0.0
        atr_pct = self._compute_atr_percentile(current_atr, atr_values)

        bar_ranges = [b["high"] - b["low"] for b in bars]
        bar_range_avg = sum(bar_ranges) / len(bar_ranges) if bar_ranges else 0.0

        regime = self._classify_regime(current_atr, atr_avg)

        state = VolatilityState(
            symbol=symbol,
            atr_current=current_atr,
            atr_average=atr_avg,
            atr_percentile=atr_pct,
            bar_range_avg=bar_range_avg,
            last_update=time.time(),
            regime=regime,
        )
        self._states[symbol] = state
        return state

    # ── Single-symbol check ──────────────────────────────────────────────

    def check_symbol(self, symbol: str) -> list[str]:
        """Check *symbol* for volatility events.

        Returns list of event description strings that were fired.
        Only fires on regime transitions (avoid noise).
        """
        state = self._states.get(symbol)
        if state is None or state.last_update == 0:
            return []

        fired: list[str] = []
        prev_regime = getattr(state, "_prev_regime", "unknown")
        current_regime = state.regime

        if current_regime == "spike" and prev_regime != "spike":
            desc = self._fire_event(EventType.VOLATILITY_SPIKE, symbol, "high", state)
            fired.append(desc)

        elif current_regime == "compress" and prev_regime != "compress":
            desc = self._fire_event(
                EventType.VOLATILITY_COMPRESS, symbol, "medium", state
            )
            fired.append(desc)

        elif current_regime == "normal" and prev_regime != "normal":
            logger.info(
                "Volatility normalised for %s (ATR=%.2f, avg=%.2f)",
                symbol,
                state.atr_current,
                state.atr_average,
            )

        state._prev_regime = current_regime  # type: ignore[attr-defined]
        return fired

    # ── Multi-symbol check ───────────────────────────────────────────────

    def check_all(self, symbols: list[str]) -> list[str]:
        """Check all symbols and fire events for regime transitions.

        Returns combined list of event descriptions.
        """
        all_fired: list[str] = []
        for sym in symbols:
            all_fired.extend(self.check_symbol(sym))
        return all_fired

    # ── ATR computation (Wilder's method) ────────────────────────────────

    @staticmethod
    def _compute_atr(bars: list[dict], period: int = 14) -> float:
        """Compute Wilder's ATR from a list of OHLCV bar dicts.

        Requires at least ``period + 1`` bars.
        """
        series = VolatilityMonitor._compute_atr_series(bars, period)
        return series[-1] if series else 0.0

    @staticmethod
    def _compute_atr_series(bars: list[dict], period: int) -> list[float]:
        """Compute a series of ATR values using Wilder's smoothing.

        Returns list of ATR values, one per bar starting from index ``period``.
        """
        if len(bars) < period + 1:
            return []

        tr_values: list[float] = []
        for i in range(1, len(bars)):
            high = bars[i]["high"]
            low = bars[i]["low"]
            prev_close = bars[i - 1]["close"]
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            tr_values.append(tr)

        if not tr_values:
            return []

        # Initial ATR = simple average of first ``period`` TR values
        initial_atr = sum(tr_values[:period]) / period
        atr_series = [initial_atr]

        # Wilder's smoothing: ATR[i] = (ATR[i-1] * (period - 1) + TR[i]) / period
        for i in range(period, len(tr_values)):
            prev_atr = atr_series[-1]
            smoothed = (prev_atr * (period - 1) + tr_values[i]) / period
            atr_series.append(smoothed)

        return atr_series

    @staticmethod
    def _compute_atr_percentile(current: float, history: list[float]) -> float:
        """Return 0-100 percentile of *current* within *history*.

        0 = lowest, 100 = highest.
        """
        if not history:
            return 50.0

        count_below = sum(1 for v in history if v < current)
        return max(0.0, min(100.0, (count_below / len(history)) * 100.0))

    # ── Regime classification ────────────────────────────────────────────

    @staticmethod
    def _classify_regime(atr_current: float, atr_average: float) -> str:
        """Classify volatility regime based on current vs average ATR."""
        if atr_average <= 0:
            return "normal"

        ratio = atr_current / atr_average
        if ratio > _SPIKE_MULTIPLIER:
            return "spike"
        if ratio < _COMPRESS_MULTIPLIER:
            return "compress"
        return "normal"

    # ── Event firing ─────────────────────────────────────────────────────

    def _fire_event(
        self,
        event_type: EventType,
        symbol: str,
        severity: str,
        state: VolatilityState,
    ) -> str:
        """Emit a volatility event and return its description."""
        data = {
            "atr_current": state.atr_current,
            "atr_average": state.atr_average,
            "atr_percentile": state.atr_percentile,
            "bar_range_avg": state.bar_range_avg,
            "regime": state.regime,
        }

        description = (
            f"{event_type.value.upper()} on {symbol}: "
            f"ATR={state.atr_current:.2f} vs avg={state.atr_average:.2f} "
            f"(pct={state.atr_percentile:.0f}%), "
            f"severity={severity}"
        )

        self._event_bus.emit(
            event_type=event_type,
            symbol=symbol,
            severity=severity,
            data=data,
        )

        logger.info("Volatility event fired: %s", description)
        return description

    # ── Monitoring loop ──────────────────────────────────────────────────

    def start_monitoring(
        self,
        symbols: list[str],
        check_interval: float = 60.0,
    ) -> asyncio.Task:
        """Start periodic background monitoring of *symbols*.

        Returns the ``asyncio.Task`` running the loop.
        """
        if self._monitoring and self._monitor_task is not None:
            logger.warning("Monitoring already active — call stop_monitoring() first")
            return self._monitor_task

        self._monitoring = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(symbols, check_interval),
            name="volatility-monitor",
        )
        logger.info(
            "VolatilityMonitor started: %d symbols, interval=%.1fs",
            len(symbols),
            check_interval,
        )
        return self._monitor_task

    def stop_monitoring(self) -> None:
        """Cancel the monitoring task."""
        self._monitoring = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
            logger.info("VolatilityMonitor stopped")

    async def _monitor_loop(
        self,
        symbols: list[str],
        interval: float,
    ) -> None:
        """Internal: periodic update + check loop. Non-blocking."""
        try:
            while self._monitoring:
                try:
                    tasks = [self.update_state(sym) for sym in symbols]
                    await asyncio.gather(*tasks, return_exceptions=True)

                    fired = self.check_all(symbols)
                    if fired:
                        logger.info(
                            "VolatilityMonitor cycle: %d events fired", len(fired)
                        )

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Error in volatility monitoring cycle for %s", symbols
                    )

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.debug("VolatilityMonitor loop cancelled")
            raise

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _extract_bars(result: dict) -> list[dict]:
        """Extract bar list from MCP response, handling various shapes.

        MCP responses typically wrap data in ``content`` → ``[0].text`` → JSON,
        but the MCP client already unwraps this. Bars may appear as:
        - ``result`` directly (list of dicts)
        - ``result["bars"]`` or ``result["data"]``
        """
        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            for key in ("bars", "data", "result", "candles"):
                val = result.get(key)
                if isinstance(val, list):
                    return val
            if "high" in result and "low" in result:
                return [result]

        logger.warning("Unexpected bars result shape: %s", type(result).__name__)
        return []

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".mt5-mcp" / "circuit_breaker.json"
JOURNAL_PATH = Path.home() / ".mt5-mcp" / "trading_journal.db"

MAX_ABSOLUTE_DAILY_LOSS_PERCENT = 0.20


@dataclass
class CircuitBreakerState:
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    daily_trades: int = 0
    open_positions: int = 0
    bridge_failures: int = 0
    last_reset: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class CircuitBreaker:
    MAX_CONSECUTIVE_LOSSES = 3
    MAX_OPEN_POSITIONS = 3
    MAX_BRIDGE_FAILURES = 3

    def __init__(self, equity: float = 200.0):
        self.state = CircuitBreakerState()
        self.equity = equity
        self.load()

    def _infer_max_risk_per_trade(self) -> float:
        """Infer max risk % from recent trade sizes in the journal.

        Reads the last 20 trade decisions, extracts confidence_level as a
        proxy for risk intent, and returns the 90th percentile. Defaults
        to 0.10 (10%) if no data is available.
        """
        try:
            if not JOURNAL_PATH.exists():
                return 0.10
            conn = sqlite3.connect(str(JOURNAL_PATH))
            cursor = conn.execute(
                "SELECT confidence_level FROM trade_decisions "
                "WHERE confidence_level IS NOT NULL AND confidence_level > 0 "
                "ORDER BY timestamp DESC LIMIT 20"
            )
            rows = cursor.fetchall()
            conn.close()
            if not rows:
                return 0.10
            confidences = [float(r[0]) for r in rows]
            confidences.sort(reverse=True)
            idx = max(0, int(len(confidences) * 0.1) - 1)
            p90 = confidences[idx]
            if p90 >= 0.8:
                return 0.10
            elif p90 >= 0.5:
                return 0.05
            return 0.02
        except Exception:
            return 0.10

    def _adaptive_daily_loss_limit(self) -> float:
        """Compute daily loss limit as 2x max single-trade risk, capped at 20%.

        This scales with Jesse's sizing strategy:
        - Risking 10%/trade → 20% daily limit (2 max-risk losses)
        - Risking 5%/trade  → 10% daily limit (2 max-risk losses)
        - Risking 2%/trade  → 5%  daily limit (2.5 max-risk losses)
        """
        max_risk = self._infer_max_risk_per_trade()
        adaptive = max_risk * 2
        return min(adaptive, MAX_ABSOLUTE_DAILY_LOSS_PERCENT)

    def check_all(self) -> tuple[bool, str | None]:
        s = self.state
        if s.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            return False, f"Cool-off: {s.consecutive_losses} consecutive losses"

        daily_loss_limit = self._adaptive_daily_loss_limit()
        max_loss = self.equity * daily_loss_limit
        if s.daily_loss >= max_loss:
            return (
                False,
                f"Daily loss limit: ${s.daily_loss:.2f} / ${max_loss:.2f} "
                f"({daily_loss_limit:.0%} of equity)",
            )

        if s.open_positions >= self.MAX_OPEN_POSITIONS:
            return False, f"Max open positions: {self.MAX_OPEN_POSITIONS}"
        if s.bridge_failures >= self.MAX_BRIDGE_FAILURES:
            return False, "Bridge disconnected"
        return True, None

    def record_trade(self, pnl: float):
        self.state.daily_trades += 1
        if pnl < 0:
            self.state.daily_loss += abs(pnl)
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        self.save()

    def record_bridge_failure(self):
        self.state.bridge_failures += 1
        self.save()

    def set_open_positions(self, count: int):
        self.state.open_positions = count
        self.save()

    def reset_daily(self):
        self.state.daily_loss = 0.0
        self.state.daily_trades = 0
        self.state.bridge_failures = 0
        self.state.consecutive_losses = 0
        self.state.last_reset = datetime.now(timezone.utc).isoformat()

    def save(self):
        """Persist circuit breaker state to JSON file."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "consecutive_losses": self.state.consecutive_losses,
                "daily_loss": self.state.daily_loss,
                "daily_trades": self.state.daily_trades,
                "open_positions": self.state.open_positions,
                "bridge_failures": self.state.bridge_failures,
                "last_reset": self.state.last_reset,
            }
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Failed to save circuit breaker state: %s", e)

    def load(self):
        """Load circuit breaker state from JSON file. Auto-resets daily."""
        try:
            if not STATE_FILE.exists():
                return
            data = json.loads(STATE_FILE.read_text())
            self.state.consecutive_losses = data.get("consecutive_losses", 0)
            self.state.daily_loss = data.get("daily_loss", 0.0)
            self.state.daily_trades = data.get("daily_trades", 0)
            self.state.open_positions = data.get("open_positions", 0)
            self.state.bridge_failures = data.get("bridge_failures", 0)
            self.state.last_reset = data.get(
                "last_reset", datetime.now(timezone.utc).isoformat()
            )

            # Auto-reset daily: compare last_reset date with today
            last_reset_dt = datetime.fromisoformat(self.state.last_reset)
            if last_reset_dt.date() != datetime.now(timezone.utc).date():
                self.state.daily_loss = 0.0
                self.state.daily_trades = 0
                self.state.bridge_failures = 0
                self.state.consecutive_losses = 0
                self.state.last_reset = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logger.warning("Failed to load circuit breaker state: %s", e)

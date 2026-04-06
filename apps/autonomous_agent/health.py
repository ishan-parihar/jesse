"""FastAPI health check endpoint for monitoring."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel

START_TIME = time.time()


class HealthResponse(BaseModel):
    status: str
    phase: str | None
    last_cycle: str | None
    open_positions: int
    daily_pnl: float
    consecutive_losses: int
    next_wake: str | None
    uptime_hours: float
    memory_count: int


_health_state: dict = {
    "phase": None,
    "last_cycle": None,
    "open_positions": 0,
    "daily_pnl": 0.0,
    "consecutive_losses": 0,
    "next_wake": None,
    "memory_count": 0,
}


def update_health(**kwargs):
    _health_state.update(kwargs)


app = FastAPI(title="Autonomous Trading Agent Health")


@app.get("/health", response_model=HealthResponse)
def health():
    uptime = (time.time() - START_TIME) / 3600
    phase = _health_state.get("phase", "UNKNOWN")
    status = "healthy" if phase not in ("ERROR", "CRASHED") else "degraded"
    return HealthResponse(
        status=status,
        phase=phase,
        last_cycle=_health_state.get("last_cycle"),
        open_positions=_health_state.get("open_positions", 0),
        daily_pnl=_health_state.get("daily_pnl", 0.0),
        consecutive_losses=_health_state.get("consecutive_losses", 0),
        next_wake=_health_state.get("next_wake"),
        uptime_hours=round(uptime, 2),
        memory_count=_health_state.get("memory_count", 0),
    )

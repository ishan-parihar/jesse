"""Heartbeat configuration types and resolution — openclaw-inspired.

Replaces the hardcoded HeartbeatConfig in heartbeat_engine.py with a
comprehensive config system supporting duration parsing, active hours,
visibility controls, and cost-awareness flags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Default prompt
# ---------------------------------------------------------------------------

HEARTBEAT_DEFAULT_PROMPT = (
    "You are Jesse's autonomous heartbeat. Review the market context below and determine if anything needs attention.\n"
    "\n"
    "Check:\n"
    "1. Are there any urgent market events since your last cycle? (price alerts hit, volatility spikes, high-impact news)\n"
    "2. Are any open positions at risk? (regime changes, key levels broken)\n"
    "3. Are there upcoming high-impact news events that should pause trading?\n"
    "4. Is the current market session optimal for trading?\n"
    "\n"
    "If nothing needs attention, reply with exactly: HEARTBEAT_OK\n"
    "If something needs attention, provide a concise alert message."
)

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"(?P<days>\d+d)?\s*"
    r"(?P<hours>\d+h)?\s*"
    r"(?P<minutes>\d+m)?\s*"
    r"(?P<seconds>\d+s)?",
    re.IGNORECASE,
)


def parse_duration(s: str) -> float:
    """Parse a duration string like '30m', '1h', '2h30m', '60s', '1d' into seconds.

    Raises ValueError on invalid input.
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty duration string")

    m = _DURATION_RE.fullmatch(s)
    if not m:
        raise ValueError(f"Invalid duration format: {s!r}")

    days = int(m.group("days")[:-1]) if m.group("days") else 0
    hours = int(m.group("hours")[:-1]) if m.group("hours") else 0
    minutes = int(m.group("minutes")[:-1]) if m.group("minutes") else 0
    seconds = int(m.group("seconds")[:-1]) if m.group("seconds") else 0

    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"Duration must be positive: {s!r}")
    return float(total)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeartbeatConfig:
    """Configuration for the LLM-driven heartbeat runner.

    Modelled after openclaw's agents.defaults.heartbeat schema.
    """

    enabled: bool = True
    every: str = "30m"
    model: str | None = None
    prompt: str | None = None
    target: str = "none"  # "none" | "last" | "telegram"
    ack_max_chars: int = 300
    active_hours_start: str | None = None  # "08:00"
    active_hours_end: str | None = None  # "22:00"
    timezone: str = "UTC"
    light_context: bool = False
    isolated_session: bool = False
    show_ok: bool = False
    show_alerts: bool = True

    @property
    def every_seconds(self) -> float:
        """Parsed interval in seconds."""
        return parse_duration(self.every)

    @property
    def effective_prompt(self) -> str:
        """Resolved prompt body — custom or default."""
        return self.prompt if self.prompt else HEARTBEAT_DEFAULT_PROMPT

    def with_overrides(self, **kwargs) -> "HeartbeatConfig":
        """Return a new config with the given fields overridden."""
        return replace(self, **kwargs)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_DEFAULT_CFG = HeartbeatConfig()


def resolve_heartbeat_config(overrides: dict | None = None) -> HeartbeatConfig:
    """Merge optional overrides onto the default heartbeat config."""
    if not overrides:
        return _DEFAULT_CFG
    valid_keys = {f.name for f in HeartbeatConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in overrides.items() if k in valid_keys}
    return _DEFAULT_CFG.with_overrides(**filtered)


# ---------------------------------------------------------------------------
# Active hours
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_time(s: str) -> int | None:
    """Parse HH:MM into minutes-since-midnight, or None."""
    m = _TIME_RE.match(s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _now_minutes_in_tz(tz_name: str, now: datetime | None = None) -> int | None:
    """Return current minutes-since-midnight in the given timezone."""
    if now is None:
        now = datetime.now(timezone.utc)

    # Python stdlib timezone lookup — try zoneinfo first (3.9+), fall back
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except (ImportError, KeyError):
        # Fallback: treat as UTC
        tz = timezone.utc

    local = now.astimezone(tz)
    return local.hour * 60 + local.minute


def is_within_active_hours(cfg: HeartbeatConfig, now: datetime | None = None) -> bool:
    """Check whether the current time falls within the configured active hours window.

    Returns True if no active hours are configured (no restriction).
    Handles overnight windows (e.g., start="22:00", end="06:00").
    """
    if not cfg.active_hours_start or not cfg.active_hours_end:
        return True

    start_min = _parse_time(cfg.active_hours_start)
    end_min = _parse_time(cfg.active_hours_end)
    if start_min is None or end_min is None:
        return True  # Invalid format → no restriction

    if start_min == end_min:
        return False  # Zero-width window → always outside

    current_min = _now_minutes_in_tz(cfg.timezone, now)
    if current_min is None:
        return True

    if end_min > start_min:
        # Normal window: 08:00 → 22:00
        return start_min <= current_min < end_min
    else:
        # Overnight window: 22:00 → 06:00
        return current_min >= start_min or current_min < end_min

"""Cron service configuration — openclaw-inspired."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_BACKOFF_SCHEDULE_MS = [30_000, 60_000, 300_000, 900_000, 3_600_000]

TRANSIENT_PATTERNS: dict[str, re.Pattern] = {
    "rate_limit": re.compile(r"(rate[_ ]limit|too many requests|429)", re.IGNORECASE),
    "overloaded": re.compile(r"\b529\b|\boverloaded\b|high demand", re.IGNORECASE),
    "network": re.compile(
        r"(network|econnreset|econnrefused|fetch failed)", re.IGNORECASE
    ),
    "timeout": re.compile(r"(timeout|etimedout)", re.IGNORECASE),
    "server_error": re.compile(r"\b5\d{2}\b"),
}


def is_transient_error(error_text: str, retry_on: list[str] | None = None) -> bool:
    if not error_text:
        return False
    keys = retry_on if retry_on else list(TRANSIENT_PATTERNS.keys())
    return any(
        TRANSIENT_PATTERNS.get(k, re.compile(r"^\b$")).search(error_text) for k in keys
    )


@dataclass(frozen=True)
class CronRetryConfig:
    max_attempts: int = 3
    backoff_ms: list[int] = field(default_factory=lambda: [30_000, 60_000, 300_000])
    retry_on: list[str] = field(
        default_factory=lambda: [
            "rate_limit",
            "overloaded",
            "network",
            "timeout",
            "server_error",
        ]
    )


@dataclass(frozen=True)
class CronConfig:
    enabled: bool = True
    store_path: str = "~/.mt5-mcp/cron/jobs.json"
    max_concurrent_runs: int = 1
    max_missed_jobs_per_restart: int = 5
    missed_job_stagger_ms: int = 5_000
    session_retention_hours: int = 24
    run_log_max_bytes: int = 2_000_000
    run_log_keep_lines: int = 2_000
    run_log_path: str = "~/.mt5-mcp/cron/runs.jsonl"
    failure_alert_after: int = 2
    failure_alert_cooldown_ms: int = 3_600_000
    retry: CronRetryConfig = field(default_factory=CronRetryConfig)

    @property
    def resolved_store_path(self) -> Path:
        return Path(self.store_path).expanduser()

    @property
    def resolved_run_log_path(self) -> Path:
        return Path(self.run_log_path).expanduser()

    def with_overrides(self, **kwargs) -> "CronConfig":
        return replace(self, **kwargs)


_DEFAULT_CRON_CFG = CronConfig()


def resolve_cron_config(overrides: dict | None = None) -> CronConfig:
    if not overrides:
        return _DEFAULT_CRON_CFG
    valid_keys = {f.name for f in CronConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in overrides.items() if k in valid_keys}
    return _DEFAULT_CRON_CFG.with_overrides(**filtered)

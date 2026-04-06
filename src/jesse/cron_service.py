"""Cron service — persistent job scheduler with openclaw semantics.

Features:
- 3 schedule types: at (one-shot), every (interval), cron (expression)
- JSON file persistence with atomic writes
- Exponential backoff on errors
- Startup catchup for missed jobs
- Run logs with auto-pruning
- Failure alerts via Telegram
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from jesse.cron_config import (
    CronConfig,
    CronRetryConfig,
    DEFAULT_BACKOFF_SCHEDULE_MS,
    TRANSIENT_PATTERNS,
    is_transient_error,
    resolve_cron_config,
)

logger = logging.getLogger(__name__)

CronScheduleKind = Literal["at", "every", "cron"]
CronRunStatus = Literal["ok", "error", "skipped"]
CronSessionTarget = Literal["main", "isolated"]
CronWakeMode = Literal["next-heartbeat", "now"]


@dataclass
class CronSchedule:
    kind: CronScheduleKind
    expr: str = ""
    every_ms: int = 0
    tz: str = "UTC"
    at_iso: str = ""

    @classmethod
    def cron(cls, expr: str, tz: str = "UTC") -> "CronSchedule":
        return cls(kind="cron", expr=expr, tz=tz)

    @classmethod
    def every(cls, ms: int) -> "CronSchedule":
        return cls(kind="every", every_ms=ms)

    @classmethod
    def at(cls, iso: str) -> "CronSchedule":
        return cls(kind="at", at_iso=iso)


@dataclass
class CronJobState:
    next_run_at_ms: float | None = None
    running_at_ms: float | None = None
    last_run_at_ms: float | None = None
    last_run_status: CronRunStatus | None = None
    last_error: str | None = None
    last_duration_ms: float | None = None
    consecutive_errors: int = 0
    last_failure_alert_at_ms: float | None = None


@dataclass
class CronJob:
    id: str
    name: str
    schedule: CronSchedule
    session_target: CronSessionTarget = "main"
    wake_mode: CronWakeMode = "next-heartbeat"
    payload: str = ""
    enabled: bool = True
    delete_after_run: bool = False
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: float = field(default_factory=lambda: time.time() * 1000)
    updated_at_ms: float = field(default_factory=lambda: time.time() * 1000)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "schedule": {
                "kind": self.schedule.kind,
                "expr": self.schedule.expr,
                "every_ms": self.schedule.every_ms,
                "tz": self.schedule.tz,
                "at_iso": self.schedule.at_iso,
            },
            "session_target": self.session_target,
            "wake_mode": self.wake_mode,
            "payload": self.payload,
            "enabled": self.enabled,
            "delete_after_run": self.delete_after_run,
            "state": {
                "next_run_at_ms": self.state.next_run_at_ms,
                "running_at_ms": self.state.running_at_ms,
                "last_run_at_ms": self.state.last_run_at_ms,
                "last_run_status": self.state.last_run_status,
                "last_error": self.state.last_error,
                "last_duration_ms": self.state.last_duration_ms,
                "consecutive_errors": self.state.consecutive_errors,
                "last_failure_alert_at_ms": self.state.last_failure_alert_at_ms,
            },
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CronJob":
        sched = d.get("schedule", {})
        job = cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            schedule=CronSchedule(
                kind=sched.get("kind", "every"),
                expr=sched.get("expr", ""),
                every_ms=sched.get("every_ms", 0),
                tz=sched.get("tz", "UTC"),
                at_iso=sched.get("at_iso", ""),
            ),
            session_target=d.get("session_target", "main"),
            wake_mode=d.get("wake_mode", "next-heartbeat"),
            payload=d.get("payload", ""),
            enabled=d.get("enabled", True),
            delete_after_run=d.get("delete_after_run", False),
            created_at_ms=d.get("created_at_ms", time.time() * 1000),
            updated_at_ms=d.get("updated_at_ms", time.time() * 1000),
        )
        sd = d.get("state", {})
        job.state = CronJobState(
            next_run_at_ms=sd.get("next_run_at_ms"),
            running_at_ms=sd.get("running_at_ms"),
            last_run_at_ms=sd.get("last_run_at_ms"),
            last_run_status=sd.get("last_run_status"),
            last_error=sd.get("last_error"),
            last_duration_ms=sd.get("last_duration_ms"),
            consecutive_errors=sd.get("consecutive_errors", 0),
            last_failure_alert_at_ms=sd.get("last_failure_alert_at_ms"),
        )
        return job


class CronStore:
    def __init__(self, config: CronConfig | None = None) -> None:
        self._config = config or resolve_cron_config()
        self._jobs: list[CronJob] = []
        self._path = self._config.resolved_store_path

    def load(self) -> None:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._save()
            return
        try:
            data = json.loads(self._path.read_text())
            self._jobs = [CronJob.from_dict(j) for j in data.get("jobs", [])]
        except Exception:
            logger.exception("Failed to load cron store, creating fresh")
            self._jobs = []
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with open(fd, "w") as f:
                json.dump({"version": 1, "jobs": [j.to_dict() for j in self._jobs]}, f)
            Path(tmp).replace(self._path)
        except Exception:
            logger.exception("Failed to save cron store")
            if tmp and Path(tmp).exists():
                Path(tmp).unlink()

    @property
    def jobs(self) -> list[CronJob]:
        return self._jobs

    @jobs.setter
    def jobs(self, value: list[CronJob]) -> None:
        self._jobs = value

    def persist(self) -> None:
        self._save()


class CronService:
    def __init__(
        self,
        config: CronConfig | None = None,
        agent=None,
        mcp_client=None,
        telegram_bot=None,
        heartbeat_runner=None,
    ) -> None:
        self._config = config or resolve_cron_config()
        self._store = CronStore(self._config)
        self._agent = agent
        self._mcp_client = mcp_client
        self._telegram_bot = telegram_bot
        self._heartbeat_runner = heartbeat_runner
        self._timer_task: asyncio.Task | None = None
        self._running = False
        self._run_log_path = self._config.resolved_run_log_path

    async def start(self) -> None:
        self._store.load()
        self._run_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._clear_stale_markers()
        await self._startup_catchup()
        self._recompute_all_next_runs()
        self._running = True
        self._timer_task = asyncio.create_task(self._timer_loop(), name="cron-timer")
        logger.info("CronService started (%d jobs)", len(self._store.jobs))

    def stop(self) -> None:
        self._running = False
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        logger.info("CronService stopped")

    async def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        payload: str,
        session_target: CronSessionTarget = "main",
        wake_mode: CronWakeMode = "next-heartbeat",
        delete_after_run: bool = False,
    ) -> CronJob:
        job = CronJob(
            id=uuid.uuid4().hex[:12],
            name=name,
            schedule=schedule,
            session_target=session_target,
            wake_mode=wake_mode,
            payload=payload,
            delete_after_run=delete_after_run,
        )
        self._compute_next_run(job)
        self._store.jobs.append(job)
        self._store.persist()
        logger.info("Cron job added: %s (%s)", job.name, job.id)
        return job

    async def update_job(self, job_id: str, **kwargs) -> CronJob | None:
        job = self._find(job_id)
        if not job:
            return None
        for key, val in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, val)
        job.updated_at_ms = time.time() * 1000
        self._compute_next_run(job)
        self._store.persist()
        return job

    async def remove_job(self, job_id: str) -> bool:
        before = len(self._store.jobs)
        self._store.jobs = [j for j in self._store.jobs if j.id != job_id]
        if len(self._store.jobs) < before:
            self._store.persist()
            return True
        return False

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        if include_disabled:
            return list(self._store.jobs)
        return [j for j in self._store.jobs if j.enabled]

    async def run_job(self, job_id: str, force: bool = False) -> dict:
        job = self._find(job_id)
        if not job:
            return {"status": "error", "error": "job not found"}
        return await self._execute_job(job)

    async def _timer_loop(self) -> None:
        try:
            while self._running:
                now_ms = time.time() * 1000
                due = [
                    j
                    for j in self._store.jobs
                    if j.enabled
                    and j.state.next_run_at_ms is not None
                    and j.state.next_run_at_ms <= now_ms
                    and j.state.running_at_ms is None
                ]

                if not due:
                    await asyncio.sleep(min(60, max(1, self._min_next_delay_s())))
                    continue

                for job in due:
                    job.state.running_at_ms = now_ms
                self._store.persist()

                for job in due:
                    await self._execute_job(job)

                self._recompute_all_next_runs()
                self._store.persist()
        except asyncio.CancelledError:
            logger.info("Cron timer loop cancelled")
            raise

    def _min_next_delay_s(self) -> float:
        now_ms = time.time() * 1000
        delays = []
        for j in self._store.jobs:
            if j.enabled and j.state.next_run_at_ms:
                d = (j.state.next_run_at_ms - now_ms) / 1000.0
                if d > 0:
                    delays.append(d)
        return min(delays) if delays else 60.0

    async def _execute_job(self, job: CronJob) -> dict:
        started = time.monotonic()
        try:
            if job.session_target == "isolated" and self._agent:
                result = await self._agent.run_autonomous_cycle(
                    context={"cron_job": job.name, "cron_payload": job.payload}
                )
                text = result.get("text", "")
            elif job.session_target == "main" and self._heartbeat_runner:
                hr = await self._heartbeat_runner.run_once(reason=f"cron:{job.id}")
                text = hr.preview or hr.reason
            else:
                text = job.payload

            elapsed_ms = (time.monotonic() - started) * 1000
            job.state.last_duration_ms = elapsed_ms
            job.state.last_run_status = "ok"
            job.state.last_run_at_ms = time.time() * 1000
            job.state.consecutive_errors = 0
            job.state.running_at_ms = None
            self._write_run_log(job, "ok")

            if job.delete_after_run:
                await self.remove_job(job.id)
                return {"status": "ok", "deleted": True}

            return {"status": "ok", "text": text[:200]}

        except Exception as e:
            elapsed_ms = (time.monotonic() - started) * 1000
            job.state.last_duration_ms = elapsed_ms
            job.state.last_run_status = "error"
            job.state.last_error = str(e)[:500]
            job.state.consecutive_errors += 1
            job.state.running_at_ms = None
            self._write_run_log(job, "error", str(e))

            backoff = self._backoff_ms(job.state.consecutive_errors)
            job.state.next_run_at_ms = time.time() * 1000 + backoff

            if job.state.consecutive_errors >= self._config.failure_alert_after:
                await self._send_failure_alert(job, str(e))

            logger.error(
                "Cron job %s failed (attempt %d): %s",
                job.id,
                job.state.consecutive_errors,
                e,
            )
            return {"status": "error", "error": str(e)[:200]}

    @staticmethod
    def _backoff_ms(errors: int) -> float:
        idx = min(errors - 1, len(DEFAULT_BACKOFF_SCHEDULE_MS) - 1)
        return DEFAULT_BACKOFF_SCHEDULE_MS[max(0, idx)]

    async def _startup_catchup(self) -> None:
        now_ms = time.time() * 1000
        missed = [
            j
            for j in self._store.jobs
            if j.enabled
            and j.state.next_run_at_ms is not None
            and j.state.next_run_at_ms < now_ms
        ]
        if not missed:
            return

        stagger = self._config.missed_job_stagger_ms / 1000.0
        for i, job in enumerate(missed[: self._config.max_missed_jobs_per_restart]):
            logger.info("Startup catchup: running missed job %s", job.id)
            await self._execute_job(job)
            job.state.next_run_at_ms = now_ms + (i + 1) * stagger * 1000

        deferred = missed[self._config.max_missed_jobs_per_restart :]
        for i, job in enumerate(deferred):
            job.state.next_run_at_ms = now_ms + (i + 1) * stagger * 1000

        self._store.persist()

    def _clear_stale_markers(self) -> None:
        now_ms = time.time() * 1000
        stale_threshold = 2 * 3600 * 1000
        for job in self._store.jobs:
            if (
                job.state.running_at_ms
                and (now_ms - job.state.running_at_ms) > stale_threshold
            ):
                logger.warning("Clearing stale running marker for job %s", job.id)
                job.state.running_at_ms = None

    def _recompute_all_next_runs(self) -> None:
        for job in self._store.jobs:
            if job.enabled and job.state.running_at_ms is None:
                self._compute_next_run(job)

    def _compute_next_run(self, job: CronJob) -> None:
        if not job.enabled:
            job.state.next_run_at_ms = None
            return
        if job.state.running_at_ms is not None:
            return

        now_ms = time.time() * 1000
        if job.schedule.kind == "every":
            last = job.state.last_run_at_ms or now_ms
            job.state.next_run_at_ms = last + job.schedule.every_ms
        elif job.schedule.kind == "at":
            if job.state.last_run_status == "ok":
                job.state.next_run_at_ms = None
                job.enabled = False
            else:
                try:
                    at_dt = datetime.fromisoformat(job.schedule.at_iso)
                    at_ms = at_dt.timestamp() * 1000
                    job.state.next_run_at_ms = at_ms if at_ms > now_ms else None
                except Exception:
                    job.state.next_run_at_ms = None
        elif job.schedule.kind == "cron":
            job.state.next_run_at_ms = self._next_cron_ms(
                job.schedule.expr, job.schedule.tz
            )

    def _next_cron_ms(self, expr: str, tz: str) -> float | None:
        try:
            from croniter import croniter

            now = datetime.now(timezone.utc)
            it = croniter(expr, now)
            next_dt = it.get_next(datetime)
            return next_dt.timestamp() * 1000
        except ImportError:
            return (time.time() + 3600) * 1000
        except Exception:
            return (time.time() + 3600) * 1000

    async def _send_failure_alert(self, job: CronJob, error: str) -> None:
        if not self._telegram_bot:
            return
        now_ms = time.time() * 1000
        last = job.state.last_failure_alert_at_ms or 0
        if now_ms - last < self._config.failure_alert_cooldown_ms:
            return
        msg = f'Cron job "{job.name}" failed {job.state.consecutive_errors} times\nLast error: {error[:200]}'
        try:
            await self._telegram_bot.send(msg, parse_mode="Text")
            job.state.last_failure_alert_at_ms = now_ms
        except Exception:
            logger.exception("Failure alert delivery failed")

    def _write_run_log(
        self, job: CronJob, status: str, error: str | None = None
    ) -> None:
        """Append a JSONL entry for this job execution, then prune if needed."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": job.id,
            "job_name": job.name,
            "status": status,
            "duration_ms": job.state.last_duration_ms,
            "error": (error[:500] if error else None),
            "consecutive_errors": job.state.consecutive_errors,
            "next_run_at_ms": job.state.next_run_at_ms,
        }
        try:
            with open(self._run_log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            logger.exception("Failed to write run log entry")
        self._prune_run_log()

    def _prune_run_log(self) -> None:
        """Trim the run log file if it exceeds size or line limits."""
        if not self._run_log_path.exists():
            return
        try:
            content = self._run_log_path.read_text()
        except Exception:
            return
        if not content:
            return

        lines = content.splitlines()
        max_bytes = self._config.run_log_max_bytes
        max_lines = self._config.run_log_keep_lines

        if len(lines) <= max_lines and self._run_log_path.stat().st_size <= max_bytes:
            return

        # Keep only the last max_lines entries
        keep = lines[-max_lines:]
        try:
            self._run_log_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._run_log_path.parent, suffix=".tmp")
            try:
                with open(fd, "w") as f:
                    for line in keep:
                        f.write(line + "\n")
                Path(tmp).replace(self._run_log_path)
            except Exception:
                if tmp and Path(tmp).exists():
                    Path(tmp).unlink()
                raise
        except Exception:
            logger.exception("Failed to prune run log")

    def _find(self, job_id: str) -> CronJob | None:
        for j in self._store.jobs:
            if j.id == job_id:
                return j
        return None

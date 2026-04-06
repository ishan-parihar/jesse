"""Tests for CronService — persistent scheduler with run logging."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
import pytest

from jesse.cron_service import (
    CronService, CronStore, CronJob, CronJobState, CronSchedule,
)
from jesse.cron_config import CronConfig, CronRetryConfig, is_transient_error


def make_config(**overrides):
    defaults = dict(
        store_path="~/.mt5-mcp/cron/jobs.json",
        run_log_path="~/.mt5-mcp/cron/runs.jsonl",
    )
    return CronConfig(**{**defaults, **overrides})


def make_temp_config(tmpdir, **overrides):
    return CronConfig(
        store_path=f"{tmpdir}/jobs.json",
        run_log_path=f"{tmpdir}/runs.jsonl",
        **overrides,
    )


def make_cron_job(name="test", schedule=None, **kwargs):
    sched = schedule or CronSchedule.every(60_000)
    return CronJob(
        id=f"job_{name}",
        name=name,
        schedule=sched,
        **kwargs,
    )


# ── CronConfig ──

def test_cron_config_defaults():
    cfg = make_config()
    assert cfg.enabled is True
    assert cfg.max_concurrent_runs == 1
    assert cfg.run_log_max_bytes == 2_000_000
    assert cfg.run_log_keep_lines == 2_000


def test_cron_config_resolved_paths():
    cfg = make_config()
    assert cfg.resolved_store_path == Path.home() / ".mt5-mcp" / "cron" / "jobs.json"
    assert cfg.resolved_run_log_path == Path.home() / ".mt5-mcp" / "cron" / "runs.jsonl"


def test_cron_config_with_overrides():
    cfg = make_config().with_overrides(enabled=False, max_concurrent_runs=3)
    assert cfg.enabled is False
    assert cfg.max_concurrent_runs == 3


def test_is_transient_error():
    assert is_transient_error("rate limit exceeded") is True
    assert is_transient_error("529 overloaded") is True
    assert is_transient_error("ECONNREFUSED") is True
    assert is_transient_error("request timeout") is True
    assert is_transient_error("502 Bad Gateway") is True
    assert is_transient_error("invalid parameter") is False
    assert is_transient_error("") is False


# ── CronStore ──

def test_store_create_fresh(tmp_path):
    cfg = make_temp_config(tmp_path)
    store = CronStore(config=cfg)
    store.load()
    assert store.jobs == []
    assert store._path.exists()


def test_store_persist_and_reload(tmp_path):
    cfg = make_temp_config(tmp_path)
    store = CronStore(config=cfg)
    store.load()
    job = make_cron_job("persist_test")
    store.jobs.append(job)
    store.persist()

    store2 = CronStore(config=cfg)
    store2.load()
    assert len(store2.jobs) == 1
    assert store2.jobs[0].name == "persist_test"
    assert store2.jobs[0].schedule.every_ms == 60_000


def test_store_serialization_roundtrip(tmp_path):
    cfg = make_temp_config(tmp_path)
    store = CronStore(config=cfg)
    store.load()

    job = CronJob(
        id="roundtrip",
        name="roundtrip_test",
        schedule=CronSchedule.every(120_000),
        session_target="isolated",
        wake_mode="now",
        payload="do the thing",
        delete_after_run=True,
    )
    job.state.last_run_status = "ok"
    job.state.consecutive_errors = 2
    job.state.last_duration_ms = 42.5
    job.state.next_run_at_ms = 1712345678901

    store.jobs.append(job)
    store.persist()

    store2 = CronStore(config=cfg)
    store2.load()
    r = store2.jobs[0]
    assert r.id == "roundtrip"
    assert r.name == "roundtrip_test"
    assert r.schedule.every_ms == 120_000
    assert r.session_target == "isolated"
    assert r.wake_mode == "now"
    assert r.payload == "do the thing"
    assert r.delete_after_run is True
    assert r.state.last_run_status == "ok"
    assert r.state.consecutive_errors == 2
    assert r.state.last_duration_ms == 42.5


# ── CronService ──

def make_service(tmp_path, **kwargs):
    cfg = make_temp_config(tmp_path)
    return CronService(config=cfg, **kwargs)


@pytest.mark.asyncio
async def test_service_start_stop(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    assert svc._running
    svc.stop()
    assert not svc._running


@pytest.mark.asyncio
async def test_add_job(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    job = await svc.add_job(
        name="my_job",
        schedule=CronSchedule.every(5_000),
        payload="scan markets",
    )
    assert job.id is not None
    assert job.name == "my_job"
    assert job.schedule.every_ms == 5_000
    assert len(svc.list_jobs()) == 1
    svc.stop()


@pytest.mark.asyncio
async def test_remove_job(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    job = await svc.add_job(
        name="removable",
        schedule=CronSchedule.every(5_000),
        payload="x",
    )
    assert await svc.remove_job(job.id)
    assert len(svc.list_jobs()) == 0
    svc.stop()


@pytest.mark.asyncio
async def test_job_persists_across_restarts(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    await svc.add_job(name="persistent", schedule=CronSchedule.every(10_000), payload="x")
    svc.stop()

    svc2 = make_service(tmp_path)
    await svc2.start()
    assert len(svc2.list_jobs()) == 1
    assert svc2.list_jobs()[0].name == "persistent"
    svc2.stop()


@pytest.mark.asyncio
async def test_execute_job_main_session(tmp_path):
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "done"})
    hb = MagicMock()
    hb.run_once = AsyncMock(return_value=MagicMock(status="ok", reason="heartbeat-ok", preview="ok"))

    svc = make_service(tmp_path, agent=agent, heartbeat_runner=hb)
    await svc.start()
    job = await svc.add_job(
        name="main_test",
        schedule=CronSchedule.every(5_000),
        payload="scan",
        session_target="main",
    )
    result = await svc.run_job(job.id)
    assert result["status"] == "ok"
    hb.run_once.assert_called_once()
    svc.stop()


@pytest.mark.asyncio
async def test_execute_job_isolated_session(tmp_path):
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "isolated done"})

    svc = make_service(tmp_path, agent=agent)
    await svc.start()
    job = await svc.add_job(
        name="isolated_test",
        schedule=CronSchedule.every(5_000),
        payload="consolidate",
        session_target="isolated",
    )
    result = await svc.run_job(job.id)
    assert result["status"] == "ok"
    agent.run_autonomous_cycle.assert_called_once()
    svc.stop()


@pytest.mark.asyncio
async def test_exponential_backoff(tmp_path):
    svc = make_service(tmp_path)
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(side_effect=RuntimeError("fail"))
    svc._agent = agent

    await svc.start()
    job = await svc.add_job(
        name="backoff_test",
        schedule=CronSchedule.every(5_000),
        payload="x",
        session_target="isolated",
    )

    # Execute and fail
    r1 = await svc.run_job(job.id)
    assert r1["status"] == "error"
    assert job.state.consecutive_errors == 1
    backoff1 = job.state.next_run_at_ms - (time.time() * 1000)
    assert backoff1 > 0

    r2 = await svc.run_job(job.id)
    assert r2["status"] == "error"
    assert job.state.consecutive_errors == 2
    backoff2 = job.state.next_run_at_ms - (time.time() * 1000)
    assert backoff2 >= backoff1

    svc.stop()


@pytest.mark.asyncio
async def test_delete_after_run(tmp_path):
    svc = make_service(tmp_path)
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "done"})
    svc._agent = agent

    await svc.start()
    job = await svc.add_job(
        name="oneshot",
        schedule=CronSchedule.every(5_000),
        payload="x",
        session_target="isolated",
        delete_after_run=True,
    )
    result = await svc.run_job(job.id)
    assert result["status"] == "ok"
    assert result["deleted"] is True
    assert len(svc.list_jobs()) == 0
    svc.stop()


@pytest.mark.asyncio
async def test_startup_catchup(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()

    job = await svc.add_job(
        name="overdue",
        schedule=CronSchedule.at("2020-01-01T00:00:00+00:00"),
        payload="catchup",
        session_target="isolated",
    )
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "caught up"})
    svc._agent = agent

    await svc.run_job(job.id)
    svc.stop()


@pytest.mark.asyncio
async def test_run_log_written_on_success(tmp_path):
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(return_value={"text": "logged"})
    svc = make_service(tmp_path, agent=agent)
    await svc.start()
    job = await svc.add_job(
        name="log_test",
        schedule=CronSchedule.every(5_000),
        payload="x",
        session_target="isolated",
    )
    await svc.run_job(job.id)
    svc.stop()

    log_path = Path(svc._run_log_path)
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["status"] == "ok"
    assert entry["job_name"] == "log_test"
    assert entry["error"] is None


@pytest.mark.asyncio
async def test_run_log_written_on_error(tmp_path):
    agent = MagicMock()
    agent.run_autonomous_cycle = AsyncMock(side_effect=RuntimeError("boom"))
    svc = make_service(tmp_path, agent=agent)
    await svc.start()
    job = await svc.add_job(
        name="error_log",
        schedule=CronSchedule.every(5_000),
        payload="x",
        session_target="isolated",
    )
    await svc.run_job(job.id)
    svc.stop()

    log_path = Path(svc._run_log_path)
    lines = log_path.read_text().strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["status"] == "error"
    assert "boom" in entry["error"]


@pytest.mark.asyncio
async def test_run_log_pruning(tmp_path):
    cfg = make_temp_config(tmp_path, run_log_keep_lines=5, run_log_max_bytes=999999)
    svc = CronService(config=cfg, agent=MagicMock())
    svc._run_log_path = Path(cfg.resolved_run_log_path)
    svc._run_log_path.parent.mkdir(parents=True, exist_ok=True)

    job = make_cron_job("prune")
    job.state.last_duration_ms = 10.0
    job.state.consecutive_errors = 0
    job.state.next_run_at_ms = time.time() * 1000 + 60000

    for i in range(20):
        job.id = f"prune_{i}"
        job.name = f"prune_{i}"
        svc._write_run_log(job, "ok")

    lines = svc._run_log_path.read_text().strip().splitlines()
    assert len(lines) == 5
    entry = json.loads(lines[-1])
    assert entry["job_name"] == "prune_19"


@pytest.mark.asyncio
async def test_schedule_cron_expression(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    job = await svc.add_job(
        name="cron_expr",
        schedule=CronSchedule.cron("*/5 * * * *", tz="UTC"),
        payload="every 5 min",
    )
    assert job.state.next_run_at_ms is not None
    assert job.state.next_run_at_ms > time.time() * 1000
    svc.stop()


def test_schedule_serialization():
    sched = CronSchedule.cron("0 9 * * 1-5", tz="Asia/Calcutta")
    job = make_cron_job("cron_ser", schedule=sched)
    d = job.to_dict()
    restored = CronJob.from_dict(d)
    assert restored.schedule.kind == "cron"
    assert restored.schedule.expr == "0 9 * * 1-5"
    assert restored.schedule.tz == "Asia/Calcutta"


def test_schedule_at_serialization():
    sched = CronSchedule.at("2025-12-01T09:00:00+00:00")
    job = make_cron_job("at_ser", schedule=sched)
    d = job.to_dict()
    restored = CronJob.from_dict(d)
    assert restored.schedule.kind == "at"
    assert "2025-12-01" in restored.schedule.at_iso


@pytest.mark.asyncio
async def test_job_disabled_skipped(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    job = await svc.add_job(
        name="disabled_job",
        schedule=CronSchedule.every(5_000),
        payload="x",
    )
    job.enabled = False
    assert len(svc.list_jobs(include_disabled=False)) == 0
    assert len(svc.list_jobs(include_disabled=True)) == 1
    svc.stop()


@pytest.mark.asyncio
async def test_update_job(tmp_path):
    svc = make_service(tmp_path)
    await svc.start()
    job = await svc.add_job(
        name="update_me",
        schedule=CronSchedule.every(60_000),
        payload="original",
    )
    updated = await svc.update_job(job.id, payload="updated", enabled=False)
    assert updated is not None
    assert updated.payload == "updated"
    assert updated.enabled is False
    svc.stop()

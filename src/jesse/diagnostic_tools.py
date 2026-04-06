"""Diagnostic and system tools.

Allows Jesse to run self-diagnostics, check ports, and validate
configuration as native tool calls.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_DATA_DIR = Path.home() / ".mt5-mcp"
_JOURNAL = _DATA_DIR / "trading_journal.db"
_CB_STATE = _DATA_DIR / "circuit_breaker.json"
_SERVICES = {
    "mcp_server": {"port": 8010, "url": "http://127.0.0.1:8010/health"},
    "tcp_bridge": {"port": 8025, "url": "http://127.0.0.1:8025/status"},
    "http_gateway": {
        "port": 8020,
        "url": "http://127.0.0.1:8020/bridge/terminal/status",
    },
    "jesse_agent": {"port": 8090, "url": "http://127.0.0.1:8090/health"},
}


def _pgrep(pattern: str) -> str | None:
    try:
        r = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip().split("\n")[0] if r.returncode == 0 else None
    except Exception:
        return None


def _systemctl_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def make_diagnostic_tools() -> list:
    """Create LangChain tools for system diagnostics."""

    @tool
    async def diagnostic_run_checks() -> str:
        """Run full system audit (matches 'jesse doctor run'). Checks: service health, PIDs, data directory, venv, env config, MT5 connection."""
        import httpx

        results = {"ok": 0, "warn": 0, "fail": 0, "checks": {}}

        # 1-4: Service health endpoints
        async with httpx.AsyncClient() as client:
            for name, info in _SERVICES.items():
                try:
                    resp = await client.get(info["url"], timeout=5)
                    ok = resp.status_code < 400
                    results["checks"][name] = {"http": ok, "port": info["port"]}
                    if ok:
                        results["ok"] += 1
                    else:
                        results["fail"] += 1
                except Exception:
                    results["checks"][name] = {"http": False, "port": info["port"]}
                    results["fail"] += 1

        # 5: Process PIDs
        agent_pid = _pgrep("jesse_agent.main")
        bridge_pid = _pgrep("tcp_bridge.main")
        results["checks"]["agent_pid"] = {"running": bool(agent_pid), "pid": agent_pid}
        results["checks"]["bridge_pid"] = {
            "running": bool(bridge_pid),
            "pid": bridge_pid,
        }
        results["ok"] += bool(agent_pid)
        if not agent_pid:
            results["warn"] += 1
        results["ok"] += bool(bridge_pid)
        if not bridge_pid:
            results["warn"] += 1

        # 6: Data directory
        data_ok = _DATA_DIR.exists()
        data_count = len(list(_DATA_DIR.iterdir())) if data_ok else 0
        results["checks"]["data_dir"] = {"exists": data_ok, "items": data_count}
        if data_ok:
            results["ok"] += 1
        else:
            results["warn"] += 1

        # 7: Virtual environment
        venv_path = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python"
        venv_ok = venv_path.exists()
        results["checks"]["venv"] = {"exists": venv_ok}
        if venv_ok:
            results["ok"] += 1
        else:
            results["fail"] += 1

        # 8: Config check
        required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
        missing = [k for k in required if not os.environ.get(k)]
        cfg_ok = len(missing) == 0
        results["checks"]["config"] = {"ok": cfg_ok, "missing": missing}
        if cfg_ok:
            results["ok"] += 1
        else:
            results["warn"] += 1

        # 9: MT5 connection
        mt5_ok = results["checks"]["mcp_server"]["http"]
        results["checks"]["mt5_connection"] = {"ok": mt5_ok}
        if mt5_ok:
            results["ok"] += 1
        else:
            results["fail"] += 1

        status = "healthy" if results["fail"] == 0 else "degraded"
        results["status"] = status
        return json.dumps(results, indent=2)

    @tool
    async def diagnostic_check_ports(ports: list[int] | None = None) -> str:
        """Check if specific ports are in use. Default: 8010, 8020, 8025, 8090."""
        if ports is None:
            ports = [8010, 8020, 8025, 8090]
        results = {}
        for port in ports:
            try:
                result = subprocess.run(
                    ["lsof", "-i", f":{port}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                results[port] = {
                    "in_use": result.returncode == 0,
                    "process": result.stdout.split("\n")[1].split()[0]
                    if result.returncode == 0 and len(result.stdout.split("\n")) > 1
                    else None,
                }
            except Exception as exc:
                results[port] = {"in_use": False, "error": str(exc)}
        return json.dumps(results, indent=2)

    @tool
    async def diagnostic_env_check() -> str:
        """Check required environment variables. Returns which are set and which are missing."""
        required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MT5_MCP_URL"]
        optional = ["MT5_GATEWAY_URL", "MT5_REDIS_URL", "JESSE_MODEL", "JESSE_BASE_URL"]

        env_status = {}
        for key in required:
            env_status[key] = {"set": bool(os.environ.get(key)), "required": True}
        for key in optional:
            env_status[key] = {"set": bool(os.environ.get(key)), "required": False}

        missing_required = [
            k for k, v in env_status.items() if v["required"] and not v["set"]
        ]
        return json.dumps(
            {
                "status": "missing_required" if missing_required else "ok",
                "missing": missing_required,
                "env": env_status,
            },
            indent=2,
        )

    @tool
    async def agent_health() -> str:
        """Get Jesse agent health status. Returns phase, uptime, PnL, consecutive losses, memory count, next wake time, open positions."""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("http://127.0.0.1:8090/health", timeout=5)
                resp.raise_for_status()
                data = resp.json()
                return json.dumps(data, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Agent health endpoint unreachable: {exc}"})

    @tool
    async def circuit_breaker_status() -> str:
        """Get circuit breaker state. Returns consecutive losses, daily loss, daily trades, open positions, bridge failures, and whether any breaker is tripped."""
        try:
            if _CB_STATE.exists():
                data = json.loads(_CB_STATE.read_text())
                return json.dumps(data, indent=2)
            return json.dumps({"status": "no state file found", "path": str(_CB_STATE)})
        except Exception as exc:
            return json.dumps({"error": f"Failed to read circuit breaker state: {exc}"})

    @tool
    async def trading_history(limit: int = 20, symbol: str = "") -> str:
        """Query recent trading decisions from the journal. Optional symbol filter. Returns timestamp, symbol, side, confidence, action, and outcome."""
        try:
            if not _JOURNAL.exists():
                return json.dumps(
                    {"error": "Trading journal not found", "path": str(_JOURNAL)}
                )
            conn = sqlite3.connect(str(_JOURNAL))
            try:
                if symbol:
                    query = "SELECT * FROM trade_decisions WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?"
                    cursor = conn.execute(query, (symbol.upper(), limit))
                else:
                    query = (
                        "SELECT * FROM trade_decisions ORDER BY timestamp DESC LIMIT ?"
                    )
                    cursor = conn.execute(query, (limit,))
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                results = [dict(zip(columns, row)) for row in rows]
                return json.dumps(
                    {"count": len(results), "trades": results}, indent=2, default=str
                )
            finally:
                conn.close()
        except Exception as exc:
            return json.dumps({"error": f"Failed to query trading journal: {exc}"})

    @tool
    async def config_get() -> str:
        """Read configuration from .env file. Returns all key-value pairs (sensitive values masked)."""
        env_file = Path(__file__).resolve().parents[3] / ".env"
        try:
            config = {}
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if "TOKEN" in key or "KEY" in key or "SECRET" in key:
                            value = value[:8] + "***" if len(value) > 8 else "***"
                        config[key] = value
            return json.dumps(config, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to read config: {exc}"})

    return [
        diagnostic_run_checks,
        diagnostic_check_ports,
        diagnostic_env_check,
        agent_health,
        circuit_breaker_status,
        trading_history,
        config_get,
    ]

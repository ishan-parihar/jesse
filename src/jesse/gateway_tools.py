"""Gateway/daemon management tools.

Allows Jesse to start/stop services, check health, and read logs
as native tool calls.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


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


def make_gateway_tools(daemon_mgr: Any) -> list:
    """Create LangChain tools for gateway/daemon management."""

    @tool
    async def gateway_service_status() -> str:
        """Check status of all Jesse services. Returns HTTP health, PID, and systemd state for each service."""
        import httpx

        services = {
            "mcp_server": {
                "pattern": "mcp_server.main",
                "systemd": "mt5-mcp-server.service",
                "url": "http://127.0.0.1:8010/health",
            },
            "tcp_bridge": {
                "pattern": "tcp_bridge.main",
                "systemd": "mt5-tcp-bridge.service",
                "url": "http://127.0.0.1:8025/status",
            },
            "http_gateway": {
                "pattern": "bridge_gateway.main",
                "systemd": "mt5-http-gateway.service",
                "url": "http://127.0.0.1:8020/bridge/terminal/status",
            },
            "jesse_agent": {
                "pattern": "autonomous_agent.main",
                "systemd": "mt5-autonomous-agent.service",
                "url": "http://127.0.0.1:8090/health",
            },
        }

        status = {}
        for name, info in services.items():
            pid = _pgrep(info["pattern"])
            systemd = _systemctl_active(info["systemd"])
            http_ok = False
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(info["url"], timeout=5)
                    http_ok = resp.status_code < 400
            except Exception:
                pass

            status[name] = {
                "pid": pid,
                "running": bool(pid),
                "systemd_active": systemd,
                "http_healthy": http_ok,
            }

        return json.dumps(status, indent=2)

    @tool
    async def gateway_service_start(service_name: str) -> str:
        """Start a Jesse service by name. Options: mt5-autonomous-agent.service, mt5-tcp-bridge.service, mt5-mcp-server.service, mt5-http-gateway.service."""
        try:
            if hasattr(daemon_mgr, "start"):
                daemon_mgr.start(service_name)
                return f"Service {service_name} started"
            return "Start not supported by daemon manager"
        except Exception as exc:
            return f"Failed to start {service_name}: {exc}"

    @tool
    async def gateway_service_stop(service_name: str) -> str:
        """Stop a Jesse service by name."""
        try:
            if hasattr(daemon_mgr, "stop"):
                daemon_mgr.stop(service_name)
                return f"Service {service_name} stopped"
            return "Stop not supported by daemon manager"
        except Exception as exc:
            return f"Failed to stop {service_name}: {exc}"

    @tool
    async def gateway_read_logs(service_name: str, lines: int = 50) -> str:
        """Read recent logs for a systemd service. Returns the last N journalctl lines."""
        try:
            result = subprocess.run(
                ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout or "No logs found"
        except Exception as exc:
            return f"Failed to read logs: {exc}"

    @tool
    async def gateway_health_check() -> str:
        """Run HTTP health check on all 4 Jesse services. Returns status code and health flag for each endpoint."""
        import httpx

        checks = {}
        async with httpx.AsyncClient() as client:
            for name, url in [
                ("mcp_server", "http://127.0.0.1:8010/health"),
                ("tcp_bridge", "http://127.0.0.1:8025/status"),
                ("http_gateway", "http://127.0.0.1:8020/bridge/terminal/status"),
                ("jesse_agent", "http://127.0.0.1:8090/health"),
            ]:
                try:
                    resp = await client.get(url, timeout=5)
                    checks[name] = {
                        "status": resp.status_code,
                        "healthy": resp.status_code < 400,
                    }
                except Exception:
                    checks[name] = {"status": "unreachable", "healthy": False}
        return json.dumps(checks, indent=2)

    return [
        gateway_service_status,
        gateway_service_start,
        gateway_service_stop,
        gateway_read_logs,
        gateway_health_check,
    ]

"""Shared utilities for Jesse CLI commands."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

HEALTH_URL = "http://127.0.0.1:8090"
MCP_URL = os.environ.get("MT5_MCP_URL", "http://127.0.0.1:8010")
PROJECT_DIR = Path(__file__).resolve().parents[2]
VENV_PYTHON = str(PROJECT_DIR / ".venv" / "bin" / "python")
SYSTEMD_SERVICE = "mt5-autonomous-agent.service"
JESSE_LOG = "/tmp/jesse.log"
TCP_BRIDGE_LOG = "/tmp/tcp-bridge.log"
DATA_DIR = Path.home() / ".mt5-mcp"


def check_health():
    """Fetch health from the running agent. Returns dict or None."""
    try:
        resp = httpx.get(f"{HEALTH_URL}/health", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def check_mcp_health():
    """Check if MCP server is reachable. Returns dict or None."""
    try:
        resp = httpx.get(f"{MCP_URL}/health", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def check_tcp_bridge():
    """Check if TCP bridge is running. Returns dict or None."""
    try:
        resp = httpx.get("http://127.0.0.1:8025/status", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def check_http_gateway():
    """Check if HTTP gateway is running. Returns dict or None."""
    try:
        resp = httpx.get("http://127.0.0.1:8020/bridge/terminal/status", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def mcp_request(tool: str, params: dict | None = None):
    """Make a direct MCP tool call via HTTP POST."""
    try:
        resp = httpx.post(
            f"{MCP_URL}/tools/{tool}",
            json=params or {},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


def get_service_status():
    """Check systemd service status. Returns dict with running, active, pid."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "mt5-jesse"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_active = result.stdout.strip() == "active"

        result2 = subprocess.run(
            ["systemctl", "is-active", SYSTEMD_SERVICE],
            capture_output=True,
            text=True,
            timeout=5,
        )
        systemd_active = result2.stdout.strip() == "active"

        result3 = subprocess.run(
            ["pgrep", "-f", "autonomous_agent.main"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pid_raw = (
            result3.stdout.strip().split("\n")[0] if result3.returncode == 0 else None
        )
        pid = pid_raw or None

        result4 = subprocess.run(
            ["pgrep", "-f", "tcp_bridge.main"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        bridge_pid_raw = (
            result4.stdout.strip().split("\n")[0] if result4.returncode == 0 else None
        )
        bridge_pid = bridge_pid_raw or None

        return {
            "jesse_gateway": is_active,
            "jesse_systemd": systemd_active,
            "agent_pid": pid,
            "bridge_pid": bridge_pid,
        }
    except Exception:
        return {
            "jesse_gateway": False,
            "jesse_systemd": False,
            "agent_pid": None,
            "bridge_pid": None,
        }


def get_env_config():
    """Read current environment config from .env and env vars."""
    env_file = PROJECT_DIR / ".env"
    env_vars = {}
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip('"').strip("'")

    for key in [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "MT5_MCP_URL",
        "JESSE_MODEL",
        "JESSE_BASE_URL",
        "JESSE_API_KEY",
    ]:
        if os.environ.get(key):
            env_vars[key] = os.environ[key]

    if "TELEGRAM_BOT_TOKEN" in env_vars:
        token = env_vars["TELEGRAM_BOT_TOKEN"]
        env_vars["TELEGRAM_BOT_TOKEN"] = (
            token[:10] + "..." + token[-4:] if len(token) > 14 else "***"
        )

    return env_vars


def save_env_config(key: str, value: str) -> bool:
    """Save a config value to .env file."""
    env_file = PROJECT_DIR / ".env"
    lines = []
    updated = False

    if env_file.is_file():
        lines = env_file.read_text().splitlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"{key}={value}")

    env_file.write_text("\n".join(new_lines) + "\n")
    return True

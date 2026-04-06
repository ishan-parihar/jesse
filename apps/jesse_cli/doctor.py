"""Full system audit and quick fixes."""

import json
import subprocess
import time
from datetime import datetime, timezone

import typer
from .utils import (
    DATA_DIR,
    HEALTH_URL,
    MCP_URL,
    PROJECT_DIR,
    VENV_PYTHON,
    check_health,
    check_http_gateway,
    check_mcp_health,
    check_tcp_bridge,
    get_env_config,
    get_service_status,
)

doctor_app = typer.Typer(help="Full system audit and quick fixes")


def _attempt_auto_fixes(svc, attempts_log):
    """Attempt automatic fixes in auto mode. Returns list of things fixed."""
    # Start agent if process not detected
    if not svc["agent_pid"] and (PROJECT_DIR / ".venv" / "bin" / "python").exists():
        subprocess.run(
            f"cd {PROJECT_DIR} && nohup {VENV_PYTHON} -m apps.jesse_agent.main > /tmp/jesse.log 2>&1 &",
            shell=True,
        )
        time.sleep(3)
        h2 = check_health()
        if h2:
            attempts_log.append("agent: started successfully")
        else:
            attempts_log.append("agent: start attempted but health check failed")

    # Attempt MCP server restart via systemctl (best-effort, no sudo required)
    if not check_mcp_health():
        for svc_name in ["mt5-mcp-server", "mt5-mcp"]:
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "restart", svc_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r.returncode == 0:
                    time.sleep(2)
                    if check_mcp_health():
                        attempts_log.append(f"mcp: restarted via {svc_name}")
                        break
            except Exception:
                continue

    # Attempt TCP bridge restart
    if not check_tcp_bridge():
        for svc_name in ["mt5-tcp-bridge", "mt5-bridge"]:
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "restart", svc_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r.returncode == 0:
                    time.sleep(2)
                    if check_tcp_bridge():
                        attempts_log.append(f"tcp_bridge: restarted via {svc_name}")
                        break
            except Exception:
                continue

    # Attempt HTTP gateway restart
    if not check_http_gateway():
        for svc_name in ["mt5-bridge-gateway", "mt5-gateway"]:
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "restart", svc_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r.returncode == 0:
                    time.sleep(2)
                    if check_http_gateway():
                        attempts_log.append(f"http_gateway: restarted via {svc_name}")
                        break
            except Exception:
                continue


def _write_auto_result(ok_count, warnings, errors, fixed, passed):
    """Write diagnosis result to watchdog directory."""
    watchdog_dir = DATA_DIR / "watchdog"
    watchdog_dir.mkdir(parents=True, exist_ok=True)
    result_path = watchdog_dir / "last-doctor-result.json"
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok_count": ok_count,
        "warnings": warnings,
        "errors": errors,
        "fixed": fixed,
        "passed": passed,
    }
    result_path.write_text(json.dumps(result, indent=2) + "\n")


@doctor_app.command("run")
def run(
    fix: bool = typer.Option(False, "--fix", help="Attempt to fix issues"),
    auto: bool = typer.Option(
        False,
        "--auto",
        "-a",
        help="Non-interactive mode — exit 0 if all OK or fixed, exit 1 on failure",
    ),
):
    """Run full system audit."""
    issues = []
    warnings = []
    ok_count = 0
    fixed = []

    # [1] Python Virtual Environment
    venv_ok = (PROJECT_DIR / ".venv" / "bin" / "python").exists()
    if venv_ok:
        ok_count += 1
    else:
        issues.append("Virtual environment not found")

    # [2] MCP Server
    mcp = check_mcp_health()
    if mcp:
        ok_count += 1
    else:
        issues.append("MCP server not reachable on :8010")

    # [3] TCP Bridge
    bridge = check_tcp_bridge()
    if bridge:
        ok_count += 1
    else:
        issues.append("TCP bridge not reachable on :8025")

    # [4] HTTP Gateway
    gw = check_http_gateway()
    if gw:
        ok_count += 1
    else:
        issues.append("HTTP gateway not reachable on :8020")

    # [5] Jesse Agent Health
    h = check_health()
    if h:
        ok_count += 1
    else:
        issues.append("Jesse agent health endpoint not reachable on :8090")

    # [6] Process Status
    svc = get_service_status()
    agent_proc_ok = bool(svc["agent_pid"])
    bridge_proc_ok = bool(svc["bridge_pid"])
    if agent_proc_ok:
        ok_count += 1
    else:
        warnings.append("Agent process not detected")
    if bridge_proc_ok:
        ok_count += 1
    else:
        warnings.append("Bridge process not detected")

    # [7] Data Directory
    data_dir_ok = DATA_DIR.exists()
    if data_dir_ok:
        ok_count += 1
    else:
        warnings.append("Data directory not found")

    # [8] Telegram Configuration
    env = get_env_config()
    telegram_ok = bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))
    if telegram_ok:
        ok_count += 1
    else:
        warnings.append("Telegram bot not configured")

    # [9] MT5 Connection
    mt5_ok = bool(mcp)
    if mt5_ok:
        ok_count += 1
    else:
        issues.append("Cannot verify MT5 connection — MCP server unreachable")

    # ── AUTO MODE: silent, fix, write result, exit ──
    if auto:
        if issues and (fix or True):  # fix is implied in auto mode
            _attempt_auto_fixes(svc, fixed)

            # Re-check after fixes
            post_mcp = check_mcp_health()
            post_bridge = check_tcp_bridge()
            post_gw = check_http_gateway()
            post_h = check_health()
            post_svc = get_service_status()

            # Rebuild issues list based on post-fix state
            post_issues = []
            if not (PROJECT_DIR / ".venv" / "bin" / "python").exists():
                post_issues.append("Virtual environment not found")
            if not post_mcp:
                post_issues.append("MCP server not reachable on :8010")
            if not post_bridge:
                post_issues.append("TCP bridge not reachable on :8025")
            if not post_gw:
                post_issues.append("HTTP gateway not reachable on :8020")
            if not post_h:
                post_issues.append("Jesse agent health endpoint not reachable on :8090")
            if not post_mcp:
                post_issues.append(
                    "Cannot verify MT5 connection — MCP server unreachable"
                )

            passed = len(post_issues) == 0
            _write_auto_result(ok_count, warnings, post_issues, fixed, passed)
            raise typer.Exit(code=0 if passed else 1)

        # No issues — all clean
        _write_auto_result(ok_count, warnings, issues, fixed, passed=True)
        raise typer.Exit(code=0)

    # ── INTERACTIVE MODE: preserve exact existing behavior ──
    typer.echo("=" * 50)
    typer.echo("  Jesse Doctor — System Audit")
    typer.echo("=" * 50)
    typer.echo()

    typer.echo("[1] Python Virtual Environment")
    if (PROJECT_DIR / ".venv" / "bin" / "python").exists():
        typer.echo("    OK: .venv exists")
    else:
        typer.echo("    FAIL: .venv not found")

    typer.echo("[2] MCP Server (:8010)")
    if mcp:
        typer.echo(f"    OK: MCP server healthy")
    else:
        typer.echo("    FAIL: MCP server not reachable")

    typer.echo("[3] TCP Bridge (:8025)")
    if bridge:
        typer.echo("    OK: TCP bridge responding")
    else:
        typer.echo("    FAIL: TCP bridge not reachable")

    typer.echo("[4] HTTP Gateway (:8020)")
    if gw:
        typer.echo("    OK: HTTP gateway healthy")
    else:
        typer.echo("    FAIL: HTTP gateway not reachable")

    typer.echo("[5] Jesse Agent Health (:8090)")
    if h:
        typer.echo(f"    OK: Agent healthy (phase: {h.get('phase', 'unknown')})")
    else:
        typer.echo("    FAIL: Agent not running or health endpoint unreachable")

    typer.echo("[6] Process Status")
    if svc["agent_pid"]:
        typer.echo(f"    OK: Agent running (PID {svc['agent_pid']})")
    else:
        typer.echo("    WARN: Agent process not running")
    if svc["bridge_pid"]:
        typer.echo(f"    OK: Bridge running (PID {svc['bridge_pid']})")
    else:
        typer.echo("    WARN: Bridge process not running")

    typer.echo("[7] Data Directory (~/.mt5-mcp)")
    if DATA_DIR.exists():
        files = list(DATA_DIR.iterdir())
        typer.echo(f"    OK: {len(files)} items in data directory")
    else:
        typer.echo("    WARN: ~/.mt5-mcp not found")

    typer.echo("[8] Telegram Configuration")
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        typer.echo("    OK: Telegram bot configured")
    else:
        typer.echo("    WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")

    typer.echo("[9] MT5 Connection")
    if mcp:
        typer.echo("    OK: MT5 bridge connected (via MCP health)")
    else:
        typer.echo("    FAIL: Cannot verify MT5 connection")

    typer.echo()
    typer.echo("=" * 50)
    typer.echo(
        f"  Results: {ok_count} OK, {len(warnings)} warnings, {len(issues)} errors"
    )
    typer.echo("=" * 50)

    if warnings:
        typer.echo()
        typer.echo("Warnings:")
        for w in warnings:
            typer.echo(f"  - {w}")

    if issues:
        typer.echo()
        typer.echo("Errors:")
        for i in issues:
            typer.echo(f"  - {i}")

        if fix:
            typer.echo()
            typer.echo("Attempting fixes...")
            if (
                not svc["agent_pid"]
                and (PROJECT_DIR / ".venv" / "bin" / "python").exists()
            ):
                typer.echo("  Starting agent...")
                subprocess.run(
                    f"cd {PROJECT_DIR} && nohup {VENV_PYTHON} -m apps.jesse_agent.main > /tmp/jesse.log 2>&1 &",
                    shell=True,
                )
                time.sleep(3)
                h2 = check_health()
                if h2:
                    typer.echo("  Agent started successfully")
                else:
                    typer.echo("  Failed to start agent — check /tmp/jesse.log")

    if not issues:
        typer.echo("\nAll checks passed!")
    elif not fix:
        typer.echo("\nRun 'jesse doctor run --fix' to attempt automatic fixes")

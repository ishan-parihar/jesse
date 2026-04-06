"""Gateway process management — start, stop, restart, status."""

import subprocess
import time

import typer
from .utils import (
    HEALTH_URL,
    PROJECT_DIR,
    VENV_PYTHON,
    SYSTEMD_SERVICE,
    JESSE_LOG,
    TCP_BRIDGE_LOG,
    check_health,
    get_service_status,
)

gateway_app = typer.Typer(help="Manage Jesse agent processes (start/stop/restart)")


@gateway_app.command()
def start(
    background: bool = typer.Option(
        True, "--foreground", "-f", help="Run in foreground"
    ),
):
    """Start the Jesse agent and TCP bridge."""
    svc = get_service_status()

    if svc["agent_pid"]:
        typer.echo(f"Jesse agent already running (PID {svc['agent_pid']})")
        return

    typer.echo("Starting TCP bridge...")
    subprocess.run(
        f"cd {PROJECT_DIR} && nohup {VENV_PYTHON} -m apps.tcp_bridge.main > {TCP_BRIDGE_LOG} 2>&1 &",
        shell=True,
    )
    time.sleep(2)

    typer.echo("Starting Jesse agent...")
    if background:
        subprocess.run(
            f"cd {PROJECT_DIR} && nohup {VENV_PYTHON} -m apps.jesse_agent.main > {JESSE_LOG} 2>&1 &",
            shell=True,
        )
        time.sleep(3)
        h = check_health()
        if h:
            typer.echo(f"Jesse agent started (phase: {h.get('phase', 'unknown')})")
        else:
            typer.echo(
                "Agent started but health endpoint not yet responsive — check logs"
            )
    else:
        subprocess.run(
            f"cd {PROJECT_DIR} && {VENV_PYTHON} -m apps.jesse_agent.main",
            shell=True,
        )


@gateway_app.command()
def stop():
    """Stop the Jesse agent and TCP bridge."""
    svc = get_service_status()

    if svc["agent_pid"]:
        typer.echo(f"Stopping Jesse agent (PID {svc['agent_pid']})...")
        subprocess.run(["kill", svc["agent_pid"]])
        time.sleep(2)
        typer.echo("Agent stopped")
    else:
        typer.echo("Jesse agent not running")

    if svc["bridge_pid"]:
        typer.echo(f"Stopping TCP bridge (PID {svc['bridge_pid']})...")
        subprocess.run(["kill", svc["bridge_pid"]])
        time.sleep(1)
        typer.echo("Bridge stopped")
    else:
        typer.echo("TCP bridge not running")


@gateway_app.command()
def restart():
    """Restart the Jesse agent and TCP bridge."""
    stop()
    time.sleep(1)
    start()


@gateway_app.command()
def status():
    """Show process status."""
    svc = get_service_status()
    h = check_health()

    typer.echo(f"Jesse Gateway: {'running' if svc['jesse_gateway'] else 'stopped'}")
    typer.echo(f"Jesse Systemd:    {'running' if svc['jesse_systemd'] else 'stopped'}")
    typer.echo(f"Agent PID:        {svc['agent_pid'] or 'not running'}")
    typer.echo(f"Bridge PID:       {svc['bridge_pid'] or 'not running'}")

    if h:
        typer.echo(f"\nHealth: {h.get('status', 'unknown')}")
        typer.echo(f"Phase: {h.get('phase', 'unknown')}")
        typer.echo(f"Uptime: {h.get('uptime_hours', 0):.1f}h")
        typer.echo(f"Positions: {h.get('open_positions', 0)}")
        typer.echo(f"Daily PnL: ${h.get('daily_pnl', 0):.2f}")
    else:
        typer.echo("\nHealth: not reachable")

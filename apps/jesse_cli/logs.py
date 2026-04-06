"""View agent and bridge logs."""

import subprocess

import typer
from .utils import JESSE_LOG, TCP_BRIDGE_LOG

logs_app = typer.Typer(help="View agent and bridge logs")


@logs_app.command("agent")
def agent(
    lines: int = typer.Option(50, "-n", "--lines", help="Number of lines"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow log output"),
):
    """View Jesse agent logs."""
    cmd = ["tail"]
    if follow:
        cmd.append("-f")
    cmd.extend(["-n", str(lines), JESSE_LOG])
    try:
        subprocess.run(cmd)
    except FileNotFoundError:
        typer.echo(f"Log file not found: {JESSE_LOG}")
        raise typer.Exit(1)


@logs_app.command("bridge")
def bridge(
    lines: int = typer.Option(50, "-n", "--lines", help="Number of lines"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow log output"),
):
    """View TCP bridge logs."""
    cmd = ["tail"]
    if follow:
        cmd.append("-f")
    cmd.extend(["-n", str(lines), TCP_BRIDGE_LOG])
    try:
        subprocess.run(cmd)
    except FileNotFoundError:
        typer.echo(f"Log file not found: {TCP_BRIDGE_LOG}")
        raise typer.Exit(1)

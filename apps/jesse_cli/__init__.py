"""Jesse CLI — Command-line interface for managing the Jesse trading agent.

Modeled after OpenClaw's CLI: gateway, doctor, status, config, positions, pnl, scan, sleep, wake, close, logs, install.
"""

import typer

app = typer.Typer(
    name="jesse",
    help="Jesse Trading Agent CLI — manage, monitor, and control your autonomous AI trader.",
    add_completion=False,
)


@app.command()
def version():
    """Show Jesse CLI version."""
    typer.echo("jesse-cli v0.1.0")


from .gateway import gateway_app  # noqa: E402
from .status import status_app  # noqa: E402
from .doctor import doctor_app  # noqa: E402
from .config import config_app  # noqa: E402
from .trading import trading_app  # noqa: E402
from .logs import logs_app  # noqa: E402
from .install import install_app  # noqa: E402

app.add_typer(
    gateway_app,
    name="gateway",
    help="Manage Jesse agent processes (start/stop/restart)",
)
app.add_typer(
    status_app, name="status", help="Agent status, health, and circuit breaker state"
)
app.add_typer(doctor_app, name="doctor", help="Full system audit and quick fixes")
app.add_typer(
    config_app, name="config", help="Environment and configuration management"
)
app.add_typer(
    trading_app,
    name="trading",
    help="Trading operations (sleep/wake/positions/pnl/scan/close)",
)
app.add_typer(logs_app, name="logs", help="View agent and bridge logs")
app.add_typer(
    install_app, name="install", help="Install systemd services and dependencies"
)


def run():
    app()


if __name__ == "__main__":
    run()

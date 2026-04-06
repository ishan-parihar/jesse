"""Install systemd services and dependencies."""

import subprocess

import typer
from .utils import PROJECT_DIR, VENV_PYTHON, SYSTEMD_SERVICE

install_app = typer.Typer(help="Install systemd services and dependencies")


@install_app.command("service")
def install_service():
    """Install systemd service for Jesse agent."""
    service_file = PROJECT_DIR / "deploy" / "systemd" / "mt5-autonomous-agent.service"
    if not service_file.exists():
        typer.echo(f"Service file not found: {service_file}")
        raise typer.Exit(1)

    import getpass

    user = getpass.getuser()
    content = service_file.read_text().replace("%USER%", user)

    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".service", delete=False) as f:
        f.write(content)
        tmp_path = f.name

    typer.echo("Installing systemd service...")
    try:
        subprocess.run(
            ["sudo", "cp", tmp_path, f"/etc/systemd/system/{SYSTEMD_SERVICE}"],
            check=True,
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        subprocess.run(["sudo", "systemctl", "enable", SYSTEMD_SERVICE], check=True)
        typer.echo(f"Service installed: {SYSTEMD_SERVICE}")
        typer.echo("Start with: sudo systemctl start mt5-autonomous-agent")
    except subprocess.CalledProcessError as e:
        typer.echo(f"Failed to install service: {e}")
        raise typer.Exit(1)
    finally:
        import os

        os.unlink(tmp_path)


@install_app.command("deps")
def install_deps():
    """Install Python dependencies."""
    typer.echo("Installing dependencies...")
    try:
        subprocess.run(
            [VENV_PYTHON, "-m", "pip", "install", "-q", "--upgrade", "pip"],
            check=True,
        )
        typer.echo("Dependencies updated")
    except subprocess.CalledProcessError as e:
        typer.echo(f"Failed to install dependencies: {e}")
        raise typer.Exit(1)

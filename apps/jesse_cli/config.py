"""Environment and configuration management."""

import typer
from .utils import get_env_config, save_env_config

config_app = typer.Typer(help="Environment and configuration management")


@config_app.command("get")
def get(key: str = typer.Argument(None, help="Specific key to show")):
    """Show current configuration."""
    env = get_env_config()
    if key:
        if key in env:
            typer.echo(f"{key}={env[key]}")
        else:
            typer.echo(f"Key not found: {key}")
            raise typer.Exit(1)
    else:
        for k, v in env.items():
            typer.echo(f"{k}={v}")


@config_app.command("set")
def set_cmd(
    key: str = typer.Argument(..., help="Config key"),
    value: str = typer.Argument(..., help="Config value"),
):
    """Set a configuration value."""
    save_env_config(key, value)
    typer.echo(f"{key}={value} (saved to .env)")
    typer.echo("Restart Jesse agent for changes to take effect: jesse gateway restart")


@config_app.command("validate")
def validate():
    """Validate configuration."""
    env = get_env_config()
    errors = []

    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    for key in required:
        if not env.get(key):
            errors.append(f"Missing required: {key}")

    if not env.get("MT5_MCP_URL"):
        typer.echo("  MT5_MCP_URL: using default (http://127.0.0.1:8010)")

    if env.get("TELEGRAM_BOT_TOKEN") and len(env["TELEGRAM_BOT_TOKEN"]) < 30:
        errors.append("TELEGRAM_BOT_TOKEN looks invalid (too short)")

    model = env.get("JESSE_MODEL", "not set")
    base_url = env.get("JESSE_BASE_URL", "not set")
    typer.echo(f"  Model: {model}")
    typer.echo(f"  Base URL: {base_url}")

    if errors:
        typer.echo("\nValidation errors:")
        for e in errors:
            typer.echo(f"  - {e}")
        raise typer.Exit(1)
    else:
        typer.echo("\nConfiguration valid")

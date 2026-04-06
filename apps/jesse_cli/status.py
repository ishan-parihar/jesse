"""Agent status, health, and circuit breaker state."""

import json

import typer
from .utils import check_health, mcp_request, get_service_status

status_app = typer.Typer(help="Agent status, health, and circuit breaker state")


@status_app.command("show")
def show():
    """Show full agent status including health, positions, and circuit breakers."""
    svc = get_service_status()
    h = check_health()

    if not h:
        typer.echo("Agent is not running. Start it with: jesse gateway start")
        return

    typer.echo(f"Status:  {h.get('status', 'unknown')}")
    typer.echo(f"Phase:   {h.get('phase', 'unknown')}")
    typer.echo(f"Uptime:  {h.get('uptime_hours', 0):.1f}h")
    typer.echo(f"Memory:  {h.get('memory_count', 0)} entries")
    typer.echo()
    typer.echo(f"Open Positions: {h.get('open_positions', 0)}")
    typer.echo(f"Daily PnL:      ${h.get('daily_pnl', 0):.2f}")
    typer.echo(f"Consec. Losses: {h.get('consecutive_losses', 0)}")
    typer.echo(f"Next Wake:      {h.get('next_wake', 'immediate')}")
    typer.echo(f"Last Cycle:     {h.get('last_cycle', 'never')}")

    typer.echo()
    typer.echo("Services:")
    typer.echo(f"  Jesse Gateway: {'running' if svc['jesse_gateway'] else 'stopped'}")
    typer.echo(f"  Agent PID:        {svc['agent_pid'] or 'not running'}")
    typer.echo(f"  Bridge PID:       {svc['bridge_pid'] or 'not running'}")


@status_app.command("health")
def health():
    """Fetch health from the running agent."""
    h = check_health()
    if h:
        typer.echo(json.dumps(h, indent=2))
    else:
        typer.echo("Agent health endpoint not reachable")
        raise typer.Exit(1)


@status_app.command("positions")
def positions():
    """List open positions."""
    result = mcp_request("positions_open")
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(1)
    positions = result if isinstance(result, list) else result.get("positions", [])
    if not positions:
        typer.echo("No open positions")
        return
    for p in positions:
        side = p.get("type", "unknown").upper()
        sym = p.get("symbol", "?")
        vol = p.get("volume", 0)
        price = p.get("price", 0)
        sl = p.get("sl", 0)
        tp = p.get("tp", 0)
        pnl = p.get("profit", 0)
        typer.echo(
            f"  {sym:10s} {side:4s} {vol:>6.2f} lots @ {price:.2f}  SL={sl:.2f} TP={tp:.2f}  PnL=${pnl:.2f}"
        )


@status_app.command("pnl")
def pnl():
    """Show recent trading performance."""
    result = mcp_request("performance_summary", {"days": 7})
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(1)
    typer.echo(json.dumps(result, indent=2, default=str))

"""Trading operations — sleep, wake, positions, pnl, scan, close."""

import json

import typer
from .utils import mcp_request

trading_app = typer.Typer(help="Trading operations")


@trading_app.command("sleep")
def sleep():
    """Pause the agent's trading cycles (equivalent to /sleep)."""
    result = mcp_request("account_summary")
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        typer.echo("Agent may not be running. Use: jesse gateway start")
        raise typer.Exit(1)
    typer.echo("Sleep mode activated — agent will stop executing trades")
    typer.echo("Use 'jesse trading wake' to resume")


@trading_app.command("wake")
def wake():
    """Resume the agent's trading cycles (equivalent to /wake)."""
    result = mcp_request("account_summary")
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(1)
    typer.echo("Wake mode activated — agent will resume trading cycles")


@trading_app.command("scan")
def scan(symbols: str = typer.Argument(None, help="Comma-separated symbols")):
    """Quick market scan (price, ATR, regime)."""
    default_symbols = ["EURUSD", "USDJPY", "GBPJPY", "XAUUSD", "BTCUSD"]
    syms = [s.strip() for s in symbols.split(",")] if symbols else default_symbols
    result = mcp_request("market_scan", {"symbols": syms, "timeframe": "H1"})
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(1)
    items = result if isinstance(result, list) else result.get("results", [])
    for item in items:
        sym = item.get("symbol", "?")
        price = item.get("price", 0)
        atr = item.get("atr", 0)
        regime = item.get("regime", "unknown")
        typer.echo(f"  {sym:10s}  {price:>10.2f}  ATR: {atr:>8.2f}  Regime: {regime}")


@trading_app.command("close")
def close(
    symbol: str = typer.Option(None, "--symbol", "-s", help="Close only this symbol"),
):
    """Close all positions (optionally filter by symbol)."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    result = mcp_request("close_all_positions", params)
    if "error" in result:
        typer.echo(f"Error: {result['error']}")
        raise typer.Exit(1)
    typer.echo(json.dumps(result, indent=2, default=str))

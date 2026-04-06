"""Startup reconciliation: detect MT5 positions without journal entries."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def reconcile_positions(
    orchestrator, mcp_client, bot=None
) -> list[dict[str, Any]]:
    """Compare MT5 open positions against journal entries on startup.

    Fetches all open positions from MT5 and checks whether each has a
    corresponding journal entry with action="entry".  Positions that lack
    a matching journal record are considered "unknown" — they were opened
    outside the agent's awareness.

    Args:
        orchestrator: AgentOrchestrator instance (unused directly but kept
            for API symmetry with the caller's intent).
        mcp_client: MCPClient used to fetch open positions.
        bot: Optional TelegramBot for sending alert messages.

    Returns:
        List of dicts, each describing an unknown position with keys:
        symbol, type, price_open, volume, position_id, profit, sl, tp.
    """
    try:
        raw_positions = await mcp_client.positions_open()
    except Exception:
        logger.exception("Failed to fetch open positions for reconciliation")
        return []

    if not raw_positions:
        logger.info("Reconciliation: no open positions — nothing to check")
        return []

    # Defensive: handle case where response is a string or dict with error
    if isinstance(raw_positions, str):
        try:
            import json as _json

            raw_positions = _json.loads(raw_positions)
        except Exception:
            logger.warning("Reconciliation: raw_positions is non-JSON string, skipping")
            return []

    if isinstance(raw_positions, dict):
        # Could be {"error": "..."} or {"data": [...]}
        if "error" in raw_positions:
            logger.warning(
                "Reconciliation: MCP returned error: %s", raw_positions["error"]
            )
            return []
        if "data" in raw_positions and isinstance(raw_positions["data"], list):
            raw_positions = raw_positions["data"]
        else:
            logger.warning("Reconciliation: unexpected dict response, skipping")
            return []

    if not isinstance(raw_positions, list):
        logger.warning(
            "Reconciliation: unexpected response type %s, skipping",
            type(raw_positions).__name__,
        )
        return []

    journaled: set[tuple[str, str]] = set()
    try:
        # Query journal via MCP HTTP API instead of direct DB import
        journal_entries = await mcp_client.trading_reflect(limit=400)
        entries = (
            journal_entries
            if isinstance(journal_entries, list)
            else journal_entries.get("decisions", [])
        )
        for entry in entries:
            sym = (entry.get("symbol") or "").strip()
            side = (entry.get("side") or "").strip().upper()
            if sym and side:
                journaled.add((sym, side))
    except Exception:
        logger.exception("Failed to query journal entries for reconciliation")

    unknown: list[dict[str, Any]] = []
    for pos in raw_positions:
        if isinstance(pos, str):
            try:
                import json as _json

                pos = _json.loads(pos)
            except Exception:
                continue
        if not isinstance(pos, dict):
            continue
        symbol = (pos.get("symbol") or "").strip()
        pos_type = (pos.get("type") or "").strip().upper()
        if not symbol or not pos_type:
            continue

        if (symbol, pos_type) not in journaled:
            unknown.append(
                {
                    "symbol": symbol,
                    "type": pos_type,
                    "price_open": pos.get("price_open"),
                    "volume": pos.get("volume"),
                    "position_id": pos.get("id") or pos.get("position_id"),
                    "profit": pos.get("profit"),
                    "sl": pos.get("sl"),
                    "tp": pos.get("tp"),
                }
            )

    if unknown:
        logger.warning(
            "Reconciliation: %d open position(s) with no journal entry: %s",
            len(unknown),
            ", ".join(f"{p['symbol']} {p['type']}" for p in unknown),
        )
    else:
        logger.info(
            "Reconciliation: all %d open position(s) have journal entries",
            len(raw_positions),
        )

    return unknown

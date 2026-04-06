"""Unified tool registry for Jesse agent.

Combines MCP-backed tools, heartbeat tools, and native tools
into a single registry that the agent can query and use.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of all tools available to Jesse."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}
        self._categories: dict[str, list[str]] = {}

    def register(self, tool: Any, category: str = "general") -> None:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        if not name:
            logger.warning("Skipping unnamed tool: %s", tool)
            return
        self._tools[name] = tool
        self._categories.setdefault(category, []).append(name)

    def register_many(self, tools: list[Any], category: str = "general") -> None:
        for t in tools:
            self.register(t, category)

    def get(self, name: str) -> Any | None:
        return self._tools.get(name)

    def get_all(self) -> list[Any]:
        return list(self._tools.values())

    def get_by_category(self, category: str) -> list[Any]:
        names = self._categories.get(category, [])
        return [self._tools[n] for n in names if n in self._tools]

    def list_tools(self) -> dict[str, list[str]]:
        return dict(self._categories)

    def tool_count(self) -> int:
        return len(self._tools)

    def build_tool_description(self) -> str:
        lines = ["AVAILABLE TOOLS:", ""]
        for category, names in sorted(self._categories.items()):
            lines.append(f"**{category.title()} Tools:**")
            for name in sorted(names):
                t = self._tools[name]
                desc = getattr(t, "description", "") or getattr(t, "__doc__", "") or ""
                lines.append(f"- `{name}`: {desc[:100]}")
            lines.append("")
        return "\n".join(lines)


def build_full_registry(
    mcp_client: Any,
    heartbeat_engine: Any | None = None,
    telegram_bot: Any | None = None,
    daemon_manager: Any | None = None,
) -> ToolRegistry:
    """Build the complete tool registry for Jesse."""
    registry = ToolRegistry()

    from jesse.agent_tools import make_mcp_tools, make_heartbeat_tools

    registry.register_many(make_mcp_tools(mcp_client), category="mt5_trading")

    if heartbeat_engine:
        registry.register_many(
            make_heartbeat_tools(heartbeat_engine), category="market_monitoring"
        )

    if telegram_bot:
        from jesse.telegram_tools import make_telegram_tools

        registry.register_many(make_telegram_tools(telegram_bot), category="telegram")

    if daemon_manager:
        from jesse.gateway_tools import make_gateway_tools

        registry.register_many(
            make_gateway_tools(daemon_manager), category="gateway_management"
        )

    from jesse.diagnostic_tools import make_diagnostic_tools

    registry.register_many(make_diagnostic_tools(), category="diagnostics")

    logger.info(
        "Tool registry built: %d tools across %d categories",
        registry.tool_count(),
        len(registry.list_tools()),
    )
    return registry

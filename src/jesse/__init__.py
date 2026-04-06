"""Autonomous trading agent module — Jesse.

Public API surface:
    MCPClient          — Async HTTP client for MT5-MCP server (port 8010)
    CircuitBreaker     — Risk management circuit breakers with JSON persistence
    HeartbeatEngine    — Event-driven adaptive heartbeat coordinator
    MarketEventBus     — Pub/sub event bus for market events
    AgentScheduler     — APScheduler + heartbeat lifecycle management
    SessionManager     — Trading session (London/NY/Sydney) detection
    VolatilityMonitor  — ATR-based volatility regime detection
    PriceAlertMonitor  — Threshold-based price alert system
    NewsEventMonitor   — Economic calendar news monitoring
    SemanticMemory     — ChromaDB vector store for learned trading patterns
    JesseAgent         — ReAct autonomous trading agent (LangChain)
    ConversationManager — Multi-user conversation state management
    TelegramBot        — Bidirectional Telegram command interface
    make_mcp_tools     — LangChain tool factory for MCP client methods
    make_heartbeat_tools — LangChain tool factory for heartbeat monitors
"""

from __future__ import annotations

__all__ = [
    # Core agent
    "JesseAgent",
    "ConversationManager",
    "TelegramBot",
    # MCP client & tools
    "MCPClient",
    "make_mcp_tools",
    "make_heartbeat_tools",
    # Heartbeat & scheduling
    "HeartbeatEngine",
    "MarketEventBus",
    "AgentScheduler",
    # Monitors
    "SessionManager",
    "VolatilityMonitor",
    "PriceAlertMonitor",
    "NewsEventMonitor",
    # Memory
    "SemanticMemory",
    # Risk management
    "CircuitBreaker",
]

from jesse.mcp_client import MCPClient
from jesse.circuit_breaker import CircuitBreaker
from jesse.heartbeat_engine import HeartbeatEngine
from jesse.market_event_bus import MarketEventBus
from jesse.scheduler import AgentScheduler
from jesse.session_manager import SessionManager
from jesse.volatility_monitor import VolatilityMonitor
from jesse.price_alert_monitor import PriceAlertMonitor
from jesse.news_event_monitor import NewsEventMonitor

# Lazy imports (heavy/external dependencies) to avoid import failures
# when optional packages (langchain, chromadb) aren't installed.


def __getattr__(name: str):
    if name == "JesseAgent":
        from jesse.react_agent import JesseAgent

        return JesseAgent
    if name == "ConversationManager":
        from jesse.conversation import ConversationManager

        return ConversationManager
    if name == "TelegramBot":
        from jesse.telegram_bot import TelegramBot

        return TelegramBot
    if name == "SemanticMemory":
        from jesse.semantic_memory import SemanticMemory

        return SemanticMemory
    if name == "make_mcp_tools":
        from jesse.agent_tools import make_mcp_tools

        return make_mcp_tools
    if name == "make_heartbeat_tools":
        from jesse.agent_tools import make_heartbeat_tools

        return make_heartbeat_tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

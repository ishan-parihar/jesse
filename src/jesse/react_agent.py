"""LangGraph ReAct agent core for Jesse — autonomous AI trading agent.

Replaces the deterministic StateGraph pipeline with a true ReAct loop:
LLM dynamically chooses which MCP tools to call, observes results, repeats
until it can act or respond.

Architecture:
  - create_react_agent(model, tools, prompt, checkpointer) as core
  - Dynamic prompts split by mode (autonomous vs conversational)
  - AsyncSqliteSaver with proper async context manager lifecycle
  - 25 MCP tools from agent_tools.py as the agent's hands
  - Circuit breaker enforcement OUTSIDE the LLM (hard boundary)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from jesse.agent_tools import (
    make_mcp_tools,
    make_heartbeat_tools,
    make_image_tools,
)
from jesse.mcp_client import MCPClient
from jesse.retry import retry_async

logger = logging.getLogger(__name__)

_AGENT_DB = Path.home() / ".mt5-mcp" / "agent_state.db"


SAFETY_PROMPT = """
POSITION SIZING DISCIPLINE (YOUR EDGE AMPLIFIER):
- Your risk per trade is DYNAMIC, not fixed. Size based on conviction.
- A+ SETUP (regime matches your proven edge + 3+ indicators agree + R:R >= 1:2 + optimal session): Risk 6-10% of equity
- A SETUP (regime favorable + 2 indicators agree + R:R >= 1:1.5): Risk 3-5% of equity
- B SETUP (some alignment but incomplete): Risk 1-2% of equity
- NO EDGE (uncertain, mixed signals, unfamiliar regime): Risk 0%. Wait.
- BEFORE every trade: call calculate_position_size with your SL distance and target risk %. Use the returned volume. Never guess lot sizes.
- Maximum 3 concurrent positions. Only open a 4th if it has higher conviction than an existing position.
"""

AUTONOMOUS_TRADING_PROMPT = (
    """You are Jesse, an autonomous AI trading agent named after Jesse Livermore — the greatest tape reader who made $100M+ from price action alone.

YOUR MODE: Autonomous trading cycle. You are running a background market analysis.

PROACTIVE AWARENESS:
You have a heartbeat system that monitors the market 24/7 between your cycles. Use get_heartbeat_context at the start of each cycle to see:
- Active market sessions (Sydney, Tokyo, London, NY, Crypto)
- Recent events (price alerts hit, volatility spikes, news events)
- Upcoming high-impact news (avoid trading 5min before NFP, CPI, FOMC)
- Volatility states (ATR normal/spike/compress for each symbol)
- Active price alerts you've set

YOUR PROCESS — PHASE 1: MANAGE EXISTING POSITIONS (do this FIRST)
1. Call positions_open — what are you currently holding?
2. For each open position:
   a) Has your thesis been invalidated? (regime changed, key level broken) → close immediately
   b) Is price moving in your favor? → call trail_position or set_trailing_stop
   c) Has price reached 1x R (profit equals your risk amount)? → consider taking 50% off, move SL to BE on remainder
   d) Is the position stagnant (no movement in 30+ min)? → consider closing to free capital
3. After managing positions, note: how many slots are open? How much margin is free?

YOUR PROCESS — PHASE 2: SCAN FOR NEW OPPORTUNITIES
4. Check heartbeat context (get_heartbeat_context) — what happened while you were idle?
5. Check account status (account_summary) — what's your current equity?
6. Scan the market for opportunities (market_scan for multi-symbol overview)
7. For promising symbols, use trading_decision_support(symbol, side) — it returns regime + ATR + RSI + EMA20 + EMA50 + coaching in ONE call (~400ms). Do NOT call market_regime + trading_context + get_indicator + trading_coach separately.
8. For deeper analysis: get_bars (OHLCV), support_resistance (S/R levels), multi_timeframe_indicators (confluence across TFs)
9. Check correlation_matrix before entering — avoid correlated exposure (e.g., long EURUSD + long GBPUSD = double USD risk)
10. Check your past performance (trading_reflect) and news (news_fetch) for context
11. Decide: TRADE or HOLD. If trading:
    a) FIRST call calculate_position_size with your SL distance and target risk % (based on conviction level)
    b) Use the EXACT volume returned. Do not adjust it.
    c) Call trading_coach to validate the setup
    d) THEN execute:
       - submit_market_order for immediate entry at current price
       - submit_pending_order for limit/stop entries when price hasn't reached your level yet (kind: buy_limit, sell_limit, buy_stop, sell_stop)
       - place_bracket_order for breakout capture in compressing markets (paired BUY STOP + SELL STOP)
12. Set price alerts for levels you want to watch (add_price_alert) — they'll wake you up when hit
13. Log your decision (log_decision)
14. Return a summary of what you did

"""
    + SAFETY_PROMPT
    + """
EDGE RECOGNITION — WHEN TO BE BOLD, WHEN TO BE PATIENT:

You are not a cautious trader. You are an edge hunter. Your job is not to avoid losses — it's to maximize the return on your edge.

BE BOLD WHEN:
- The market regime matches your proven edge (check your LEARNED PATTERNS)
- 3+ indicators align (trend, momentum, volatility all agree)
- R:R is >= 1:2 (you're risking $1 to make $2+)
- You're in a high-volatility session (London, NY, or London-NY overlap)
- Your recent win rate on this setup type is > 55%
These are A+ setups. Size 6-10%. Execute decisively.

BE PATIENT WHEN:
- Regime is unclear or doesn't match your edge
- Only 1-2 indicators agree (weak confluence)
- R:R is < 1:1.5 (not worth the risk)
- You're in a low-volatility session (Sydney, late Tokyo)
- You've had 2+ consecutive losses (you may be tilting)
Reduce size to 1-2% or skip entirely. The market will give another opportunity.

YOUR EDGE IS NOT CONSTANT. It varies by symbol, regime, session, and your mental state. Your sizing should reflect this. A+ setups deserve A+ sizing. Mediocre setups deserve zero.

REMEMBER: The greatest traders in history made their fortunes by betting BIG when they had an edge and betting NOTHING when they didn't. Not by betting small every time.

TRADING SCHEDULE:
- Weekdays (Mon-Fri): EURUSD, USDJPY, GBPJPY, AUDUSD, US30, XAUUSD, USOIL, BTCUSD
- Weekends (Sat-Sun): BTCUSD, ETHUSD only

SESSION AWARENESS:
- Sydney (22:00-07:00 UTC): Low vol, AUD/NZD pairs active
- Tokyo (00:00-09:00 UTC): Medium vol, JPY pairs active
- London (08:00-17:00 UTC): High vol, EUR/GBP pairs — best trading hours
- New York (13:00-22:00 UTC): High vol, USD pairs — London overlap (13:00-17:00) = peak
- London-NY overlap: Highest volatility, best breakout opportunities
- Avoid trading during low-vol sessions unless you have a specific edge

RISK MANAGEMENT (DYNAMIC, NOT FIXED):
- Determine SL from market structure: ATR multiples, support/resistance, swing highs/lows
- Determine TP from risk:reward minimum 1:1.5, target 1:2+
- BEFORE executing: call calculate_position_size with your SL distance and desired risk % (based on conviction level)
- Use the EXACT volume returned by calculate_position_size. Do not adjust it.
- Explain WHY you chose each level and WHY this conviction level

ORDER MANAGEMENT:
- Use orders_pending to check existing pending orders before placing new ones
- Use cancel_order or cancel_all_orders to remove stale pending orders
- Use modify_order to adjust pending order price/SL/TP
- Use modify_position_sl_tp to trail stops on open positions
- Use set_trailing_stop for server-side auto-trailing based on ATR

FINAL OUTPUT FORMAT (after all tool calls):
Summarize your actions concisely:
- If traded: "TRADED: SYMBOL SIDE @ PRICE, SL=level, TP=level, vol=lots. Rationale: ..."
- If held: "HOLD: No clear edge detected. Market conditions: ..."
- If pending order placed: "PENDING: SYMBOL kind @ PRICE, SL=level, TP=level. Rationale: ..."
- Include any alerts or concerns
"""
)

CONVERSATIONAL_PROMPT = (
    """You are Jesse, an autonomous AI trading agent named after Jesse Livermore.

YOUR MODE: Conversational. The user is asking you a question or giving instructions.

You can use your tools to:
- Check account status (account_summary, positions_open)
- Analyze markets:
  - trading_decision_support(symbol, side) — regime + ATR + RSI + EMAs + coaching in ONE call (preferred)
  - market_scan for multi-symbol overview
  - support_resistance for S/R level detection
  - correlation_matrix to check correlated exposure before entering
  - get_bars, get_indicator, multi_timeframe_indicators for deeper analysis
- Review past performance (trading_reflect, trading_insights)
- Check news (news_fetch, get_upcoming_news)
- Check heartbeat context (get_heartbeat_context) — recent events, active sessions, volatility
- Execute trades:
  - submit_market_order for immediate market entry
  - submit_pending_order for limit/stop entries (kind: buy_limit, sell_limit, buy_stop, sell_stop)
  - place_bracket_order for breakout setups (paired BUY STOP + SELL STOP)
  - orders_pending to list pending orders
  - cancel_order / cancel_all_orders to remove pending orders
  - modify_order to adjust pending order price/SL/TP
- Manage positions:
  - close_position to close a single position
  - close_all_positions to close everything (optional symbol/side filter)
  - modify_position_sl_tp to adjust SL/TP on open positions
  - set_trailing_stop for server-side auto-trailing
- Set price alerts (add_price_alert, remove_price_alert, list_price_alerts)
- Get coaching feedback (trading_coach)
- Validate setups (validate_trade_setup) and calculate position size (calculate_position_size)
- Check correlation_matrix to avoid correlated exposure
- Get S/R levels (support_resistance) for precise SL/TP placement
- Analyze charts visually:
  - analyze_chart(symbol, timeframe) — get chart screenshot and analyze market structure with vision
  - image_analyze(image_source, prompt) — analyze any image file path or URL

"""
    + SAFETY_PROMPT
    + """
RESPONSE STYLE:
- Be concise and professional
- Use data from your tools to support your answers
- If the user asks to trade, confirm the details before executing
- If asked about performance, provide specific numbers
- If you don't know something, say so — don't hallucinate
"""
)


def _build_system_prompt(
    mode: str, context: dict | None = None, memory_rules: list[str] | None = None
) -> str:
    if mode == "autonomous":
        base = AUTONOMOUS_TRADING_PROMPT
    else:
        base = CONVERSATIONAL_PROMPT

    context_parts = []

    if context:
        if "circuit_breaker" in context:
            ok, reason = context["circuit_breaker"]
            if not ok:
                context_parts.append(f"CIRCUIT BREAKER ACTIVE: {reason}")

        if "active_symbols" in context:
            syms = ", ".join(context["active_symbols"])
            context_parts.append(f"Active symbols for this session: {syms}")

        if "account_brief" in context:
            context_parts.append(f"Account: {context['account_brief']}")

        if "active_sessions" in context:
            context_parts.append(
                f"Active sessions: {', '.join(context['active_sessions'])}"
            )

        if "session_volatility_hint" in context:
            context_parts.append(
                f"Market volatility: {context['session_volatility_hint']}"
            )

        if "recent_events" in context and context["recent_events"]:
            context_parts.append("Recent market events:")
            for evt in context["recent_events"][:5]:
                context_parts.append(f"  • {evt}")

    if memory_rules:
        context_parts.append(
            "LEARNED PATTERNS FROM YOUR TRADING HISTORY:\n"
            + "\n".join(f"  - {r}" for r in memory_rules)
        )

    if context_parts:
        return base + "\n\nRUNTIME CONTEXT:\n" + "\n".join(context_parts)

    return base


def _build_runtime_context(context: dict | None = None) -> str:
    if not context:
        return ""

    parts = []

    if "circuit_breaker" in context:
        ok, reason = context["circuit_breaker"]
        if not ok:
            parts.append(f"CIRCUIT BREAKER ACTIVE: {reason}")

    if "active_symbols" in context:
        syms = ", ".join(context["active_symbols"])
        parts.append(f"Active symbols for this session: {syms}")

    if "account_brief" in context:
        parts.append(f"Account: {context['account_brief']}")

    if "active_sessions" in context:
        parts.append(f"Sessions: {', '.join(context['active_sessions'])}")

    if "session_volatility_hint" in context:
        parts.append(f"Volatility: {context['session_volatility_hint']}")

    return "\n".join(parts)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(p for p in parts if p)
    return str(content)


def _extract_final_response(result: dict) -> dict:
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return {
                "text": _text_from_content(msg.content),
                "messages": messages,
                "full_result": result,
            }
    return {
        "text": "No response generated",
        "messages": messages,
        "full_result": result,
    }


class JesseAgent:
    """LangGraph ReAct agent wrapper for Jesse.

    Prompt is injected per-invocation (not at init) so autonomous and
    conversational modes use different system prompts.
    """

    def __init__(self, llm, tools, checkpointer):
        from langgraph.prebuilt import create_react_agent

        self._agent = create_react_agent(
            model=llm,
            tools=tools,
            checkpointer=checkpointer,
        )
        self._llm = llm
        self._tools = tools
        self._checkpointer = checkpointer

    async def _sanitize_checkpoint(self, thread_id: str) -> bool:
        """Delete corrupted checkpoint state for a thread_id.

        LangGraph's SQLite checkpoint stores conversation history including
        AIMessage tool_calls and their ToolMessage responses. When a crash
        occurs mid-tool-execution, orphaned AIMessage entries (tool_calls
        without matching ToolMessages) remain. On the next invoke, LangGraph
        validates the history and raises INVALID_CHAT_HISTORY.

        This method purges ALL checkpoint data for the thread, giving a
        clean slate. Conversation history is lost but the agent becomes
        functional again.

        Returns True if cleanup was performed, False if no checkpoint found.
        """
        try:
            conn = getattr(self._checkpointer, "_conn", None)
            if conn is None:
                logger.warning("Cannot access checkpointer connection for sanitization")
                return False

            cursor = await conn.execute(
                "DELETE FROM writes WHERE thread_id = ?",
                (thread_id,),
            )
            writes_deleted = cursor.rowcount

            cursor = await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?",
                (thread_id,),
            )
            checkpoints_deleted = cursor.rowcount
            await conn.commit()

            if writes_deleted or checkpoints_deleted:
                logger.info(
                    "Sanitized checkpoint for thread %s: %d checkpoints, %d writes removed",
                    thread_id,
                    checkpoints_deleted,
                    writes_deleted,
                )
                return True

            logger.debug("No checkpoint found for thread %s to sanitize", thread_id)
            return False

        except Exception as exc:
            logger.error(
                "Checkpoint sanitization failed for %s: %s",
                thread_id,
                exc,
                exc_info=True,
            )
            return False

    async def _safe_invoke(
        self, messages: list, config: dict, max_retries: int = 1
    ) -> dict:
        """Invoke the agent with robust error handling for corrupted checkpoints.

        Error handling cascade:
        1. Normal invoke — works 99% of the time
        2. On INVALID_CHAT_HISTORY → sanitize checkpoint → retry once
        3. On sanitize failure → retry with fresh ephemeral thread_id
        4. On any other exception → raise with enriched context

        Args:
            messages: List of LangChain messages to invoke with
            config: LangGraph config dict with thread_id
            max_retries: Number of retry attempts after sanitization (default 1)

        Returns:
            Dict with agent response (same format as _extract_final_response input)

        Raises:
            Exception: Re-raises non-checkpoint errors after logging diagnostics
        """
        thread_id = config.get("configurable", {}).get("thread_id", "unknown")
        attempt = 0

        while True:
            try:
                return await retry_async(
                    fn=lambda: self._agent.ainvoke(
                        {"messages": messages}, config=config
                    ),
                    attempts=3,
                    label=f"agent-invoke-{thread_id[:20]}",
                    on_retry=lambda info: logger.warning(
                        "Retrying agent invoke (attempt %d/%d, delay %.1fs): %s",
                        info["attempt"],
                        info["max_attempts"],
                        info["delay"],
                        info["err"],
                    ),
                )

            except ValueError as exc:
                error_str = str(exc).lower()
                is_checkpoint_corruption = (
                    "tool" in error_str
                    and ("toolmessage" in error_str or "tool_calls" in error_str)
                    and ("invalid" in error_str or "corresponding" in error_str)
                )

                if not is_checkpoint_corruption:
                    logger.error(
                        "Non-checkpoint ValueError in agent invoke (thread=%s): %s",
                        thread_id,
                        exc,
                        exc_info=True,
                    )
                    raise

                if attempt >= max_retries:
                    fresh_id = f"ephemeral:{uuid.uuid4().hex[:12]}"
                    logger.warning(
                        "Checkpoint sanitization exhausted for %s, falling back to fresh thread %s",
                        thread_id,
                        fresh_id,
                    )
                    fresh_config = {"configurable": {"thread_id": fresh_id}}
                    return await self._agent.ainvoke(
                        {"messages": messages}, config=fresh_config
                    )

                attempt += 1
                logger.warning(
                    "Detected corrupted checkpoint for thread %s (attempt %d/%d). Sanitizing...",
                    thread_id,
                    attempt,
                    max_retries + 1,
                )

                sanitized = await self._sanitize_checkpoint(thread_id)
                if not sanitized:
                    fresh_id = f"ephemeral:{uuid.uuid4().hex[:12]}"
                    logger.warning(
                        "No checkpoint to sanitize for %s, retrying with fresh thread %s",
                        thread_id,
                        fresh_id,
                    )
                    config = {"configurable": {"thread_id": fresh_id}}

            except Exception as exc:
                logger.error(
                    "Unexpected error in agent invoke (thread=%s, attempt=%d): %s",
                    thread_id,
                    attempt,
                    exc,
                    exc_info=True,
                )
                raise

    async def run_autonomous_cycle(self, context: dict | None = None) -> dict:
        memory_rules = self._fetch_memory_rules(context)
        is_heartbeat = context.get("heartbeat_mode") if context else False
        system = _build_system_prompt("autonomous", context, memory_rules)
        runtime_context = _build_runtime_context(context)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if is_heartbeat:
            market_ctx = context.get("market_context", "")
            user_content = (
                f"[{today}] HEARTBEAT CHECK.\n"
                + (f"Market context:\n{market_ctx}\n\n" if market_ctx else "")
                + "Review the context above. If anything needs attention, provide a concise alert. "
                + "If nothing needs attention, reply with exactly: HEARTBEAT_OK"
            )
            thread_id = f"hb:{uuid.uuid4().hex[:12]}"
            logger.info("Starting heartbeat cycle (thread=%s)", thread_id[:20])
        else:
            user_content = (
                f"[{today}]"
                + (f" {runtime_context}" if runtime_context else "")
                + " Run your autonomous trading cycle. Analyze the market and act accordingly."
            )
            thread_id = f"auto:{uuid.uuid4().hex[:12]}"
            logger.info("Starting autonomous trading cycle (thread=%s)", thread_id[:20])

        config = {"configurable": {"thread_id": thread_id}}

        result = await self._safe_invoke(
            messages=[
                SystemMessage(content=system),
                HumanMessage(content=user_content),
            ],
            config=config,
        )
        return _extract_final_response(result)

    def _fetch_memory_rules(self, context: dict | None) -> list[str]:
        try:
            from jesse.semantic_memory import SemanticMemory

            memory = SemanticMemory()
            rules = memory.get_active_rules()
            return [r["text"] for r in rules[:5]]
        except Exception as exc:
            logger.debug("No learned rules available: %s", exc)
            return []

    async def run_conversation(
        self,
        user_message: str,
        chat_id: str,
        context: dict | None = None,
    ) -> dict:
        system = _build_system_prompt("conversational", context)
        thread_id = f"telegram:{chat_id}"
        config = {"configurable": {"thread_id": thread_id}}

        prefix = (
            f"{_build_runtime_context(context)}\n\n"
            if _build_runtime_context(context)
            else ""
        )
        logger.info("Conversation with %s: %s", chat_id, user_message[:80])

        try:
            result = await self._safe_invoke(
                messages=[
                    SystemMessage(content=system),
                    HumanMessage(content=prefix + user_message),
                ],
                config=config,
            )
            return _extract_final_response(result)

        except Exception as exc:
            logger.error(
                "Conversation failed after all retries (chat=%s, thread=%s): %s",
                chat_id,
                thread_id,
                exc,
                exc_info=True,
            )
            return {
                "text": (
                    f"⚠️ I encountered an error while processing your request. "
                    f"Technical details: {type(exc).__name__}. "
                    f"Please try again in a moment."
                ),
                "messages": [],
                "full_result": {"error": str(exc), "error_type": type(exc).__name__},
            }

    async def run_user_command(
        self,
        command: str,
        params: dict,
        chat_id: str = "cli",
        context: dict | None = None,
    ) -> dict:
        system = _build_system_prompt("conversational", context)
        thread_id = f"user_cmd:{command}:{chat_id}"
        config = {"configurable": {"thread_id": thread_id}}

        instruction = f"Execute the following user command: {command}"
        if params:
            instruction += f" with parameters: {json.dumps(params)}"
        instruction += "\nExecute this command now and report the result concisely."

        logger.info(
            "User command via agent: %s params=%s chat=%s", command, params, chat_id
        )

        try:
            result = await self._safe_invoke(
                messages=[
                    SystemMessage(content=system),
                    HumanMessage(content=instruction),
                ],
                config=config,
            )
            return _extract_final_response(result)
        except Exception as exc:
            logger.error(
                "User command failed (command=%s, chat=%s): %s",
                command,
                chat_id,
                exc,
                exc_info=True,
            )
            return {
                "text": f"Command execution failed: {type(exc).__name__}",
                "messages": [],
                "full_result": {"error": str(exc), "error_type": type(exc).__name__},
            }


class AgentResources:
    """Holds all agent dependencies with proper lifecycle management."""

    def __init__(
        self,
        mcp_client: MCPClient,
        checkpointer,
        agent: JesseAgent,
        db_conn=None,
        failover_chain=None,
    ):
        self.mcp_client = mcp_client
        self.checkpointer = checkpointer
        self.agent = agent
        self._db_conn = db_conn
        self.failover_chain = failover_chain

    async def close(self):
        if self._db_conn:
            try:
                await self._db_conn.close()
            except Exception:
                logger.warning("DB connection close failed", exc_info=True)
        try:
            await self.mcp_client.close()
        except Exception:
            logger.warning("MCP client close failed", exc_info=True)


def _resolve_llm_params() -> tuple[str, str, str]:
    return (
        os.environ.get("JESSE_MODEL", "coder-model"),
        os.environ.get("JESSE_BASE_URL", "http://127.0.0.1:3000/v1"),
        os.environ.get("JESSE_API_KEY", "not-needed"),
    )


async def create_agent_resources(
    db_path: Path | str | None = None,
    llm=None,
    heartbeat_engine=None,
    telegram_bot=None,
    daemon_manager=None,
) -> AgentResources:
    import aiosqlite
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    failover_chain = None
    if llm is None:
        from jesse.model_fallback import (
            create_failover_chain,
            create_llm_from_entry,
            get_current_model,
        )

        failover_chain = create_failover_chain()
        llm = create_llm_from_entry(get_current_model(failover_chain))
        logger.info(
            "LLM failover chain: %d models configured", len(failover_chain.entries)
        )

    db = db_path or _AGENT_DB
    Path(db).parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(db))
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.commit()

    checkpointer = AsyncSqliteSaver(conn)

    mcp_url = os.environ.get("MT5_MCP_URL", "http://127.0.0.1:8010")
    mcp_client = MCPClient(base_url=mcp_url)
    tools = make_mcp_tools(mcp_client)
    tools.extend(make_heartbeat_tools(heartbeat_engine))

    if telegram_bot:
        from jesse.telegram_tools import make_telegram_tools

        tools.extend(make_telegram_tools(telegram_bot))

    if daemon_manager:
        from jesse.gateway_tools import make_gateway_tools

        tools.extend(make_gateway_tools(daemon_manager))

    from jesse.diagnostic_tools import make_diagnostic_tools

    tools.extend(make_diagnostic_tools())

    tools.extend(make_image_tools(mcp_client, llm))

    agent = JesseAgent(
        llm=llm,
        tools=tools,
        checkpointer=checkpointer,
    )

    logger.info("Agent initialized with %d tools", len(tools))

    return AgentResources(
        mcp_client=mcp_client,
        checkpointer=checkpointer,
        agent=agent,
        db_conn=conn,
        failover_chain=failover_chain,
    )

"""LangChain tool wrappers for agentic MCP tool selection."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def make_mcp_tools(client: Any) -> list:
    """Create LangChain tools from MCPClient methods."""

    @tool
    async def market_regime(symbol: str, timeframe: str = "H1") -> str:
        """Detect market regime: trending_up, trending_down, ranging, or compressing."""
        result = await client.market_regime(symbol, timeframe)
        return json.dumps(result, default=str)

    @tool
    async def trading_context(symbol: str) -> str:
        """Get live trading context: ATR, volatility, current price, spread. Use for position sizing and SL/TP."""
        result = await client.trading_context(symbol)
        return json.dumps(result, default=str)

    @tool
    async def get_bars(symbol: str, timeframe: str = "H1", count: int = 100) -> str:
        """Fetch OHLCV candles for a symbol and timeframe."""
        result = await client.get_bars(symbol, timeframe, count)
        return json.dumps(result, default=str)

    @tool
    async def get_indicator(
        symbol: str,
        timeframe: str,
        indicator: str,
        period: int = 14,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> str:
        """Fetch indicator: rsi, macd, ema, sma, bbands, atr, stoch, adx, ichimoku, cci, obv."""
        result = await client.get_indicator(
            symbol,
            timeframe,
            indicator,
            period=period,
            fast=fast,
            slow=slow,
            signal=signal,
        )
        return json.dumps(result, default=str)

    @tool
    async def market_scan(symbols: list[str], timeframe: str = "H1") -> str:
        """Multi-symbol scan: returns price, ATR, regime, recommendation for all symbols at once."""
        result = await client.market_scan(symbols=symbols, timeframe=timeframe)
        return json.dumps(result, default=str)

    @tool
    async def trading_coach(
        symbol: str,
        side: str,
        sl_distance_points: float | None = None,
        tp_distance_points: float | None = None,
    ) -> str:
        """Advisory coaching: checks SL/ATR ratio, risk:reward, trend alignment."""
        result = await client.trading_coach(
            symbol,
            side,
            sl_distance_points=sl_distance_points,
            tp_distance_points=tp_distance_points,
        )
        return json.dumps(result, default=str)

    @tool
    async def trading_reflect(
        symbol: str | None = None,
        outcome: str | None = None,
        regime: str | None = None,
        limit: int = 10,
    ) -> str:
        """Query past trading decisions. Check historical performance with symbols, regimes, mistakes."""
        result = await client.trading_reflect(
            symbol=symbol,
            outcome=outcome,
            regime=regime,
            limit=limit,
        )
        return json.dumps(result, default=str)

    @tool
    async def news_fetch(limit: int = 10, keywords: list[str] | None = None) -> str:
        """Fetch latest financial news headlines with optional keyword filtering."""
        result = await client.news_fetch(
            pools=["FINANCIAL_MARKETS"],
            limit=limit,
            keywords=keywords,
        )
        return json.dumps(result, default=str)

    @tool
    async def positions_open() -> str:
        """List all open positions with PnL, SL, TP, volume."""
        result = await client.positions_open()
        return json.dumps(result, default=str)

    @tool
    async def account_summary() -> str:
        """Get account summary: balance, equity, margin, free margin."""
        result = await client.account_summary()
        return json.dumps(result, default=str)

    @tool
    async def submit_market_order(
        symbol: str,
        side: str,
        volume_lots: float,
        sl: float | None = None,
        tp: float | None = None,
    ) -> str:
        """Submit a market order. DEMO ONLY."""
        import uuid

        result = await client.submit_market_order(
            intent_id=str(uuid.uuid4()),
            strategy_id="autonomous_v1",
            account_id="demo",
            symbol=symbol,
            side=side,
            order_kind="market",
            volume_lots=volume_lots,
            sl=sl,
            tp=tp,
        )
        return json.dumps(result, default=str)

    @tool
    async def close_position(position_id: str, volume: float | None = None) -> str:
        """Close an open position. Pass position_id. Optional volume for partial close."""
        result = await client.close_position(position_id, volume)
        return json.dumps(result, default=str)

    @tool
    async def volatility_profile(
        symbol: str,
        timeframe: str = "H1",
        lookback: int = 20,
    ) -> str:
        """Get volatility summary: ATR, bar ranges, spread analysis."""
        result = await client.volatility_profile(symbol, timeframe, lookback)
        return json.dumps(result, default=str)

    @tool
    async def log_decision(
        symbol: str,
        side: str,
        action: str,
        confidence_level: float = 0.5,
        model_justification: str = "",
        regime: str = "unknown",
        sl: float | None = None,
        tp: float | None = None,
    ) -> str:
        """Log a trading decision."""
        result = await client.trading_log_decision(
            symbol=symbol,
            side=side,
            action=action,
            confidence_level=confidence_level,
            model_justification=model_justification,
            regime=regime,
            sl=sl,
            tp=tp,
        )
        return json.dumps(result, default=str)

    @tool
    async def submit_pending_order(
        symbol: str,
        side: str,
        kind: str,
        price: float,
        volume_lots: float,
        sl: float | None = None,
        tp: float | None = None,
        deviation: int = 20,
    ) -> str:
        """Submit a pending (limit/stop) order. kind is one of: buy_limit, sell_limit, buy_stop, sell_stop."""
        result = await client.submit_pending_order(
            symbol=symbol,
            side=side,
            kind=kind,
            price=price,
            volume_lots=volume_lots,
            sl=sl,
            tp=tp,
            deviation=deviation,
        )
        return json.dumps(result, default=str)

    @tool
    async def orders_pending() -> str:
        """List all pending orders."""
        result = await client.orders_pending()
        return json.dumps(result, default=str)

    @tool
    async def cancel_order(order_id: str) -> str:
        """Cancel a single pending order by its order_id."""
        result = await client.cancel_order(order_id)
        return json.dumps(result, default=str)

    @tool
    async def cancel_all_orders(
        symbol: str | None = None, side: str | None = None
    ) -> str:
        """Cancel all pending orders. Optional: filter by symbol and/or side."""
        result = await client.cancel_all_orders(symbol=symbol, side=side)
        return json.dumps(result, default=str)

    @tool
    async def modify_order(
        order_id: str,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> str:
        """Modify a pending order's price, SL, or TP. Only works on pending orders, not open positions."""
        result = await client.modify_order(
            order_id, new_price=price, new_sl=sl, new_tp=tp
        )
        return json.dumps(result, default=str)

    @tool
    async def modify_position_sl_tp(
        position_id: str,
        sl: float | None = None,
        tp: float | None = None,
    ) -> str:
        """Modify SL/TP on an open position."""
        result = await client.modify_position_sl_tp(position_id, sl=sl, tp=tp)
        return json.dumps(result, default=str)

    @tool
    async def close_all_positions(
        symbol: str | None = None, side: str | None = None
    ) -> str:
        """Close all open positions. Optional: filter by symbol and/or side."""
        result = await client.close_all_positions(symbol=symbol, side=side)
        return json.dumps(result, default=str)

    @tool
    async def validate_trade_setup(
        symbol: str,
        side: str,
        volume_lots: float,
        entry_price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        order_kind: str = "market",
    ) -> str:
        """Validate a trade setup against broker constraints (min volume, max volume, stop levels)."""
        result = await client.validate_trade_setup(
            symbol=symbol,
            side=side,
            order_kind=order_kind,
            volume_lots=volume_lots,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
        )
        return json.dumps(result, default=str)

    @tool
    async def calculate_position_size(
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        risk_percent: float = 1.0,
        equity: float | None = None,
    ) -> str:
        """Calculate risk-based position size. Returns lot size for given risk % and SL distance."""
        result = await client.calculate_position_size(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            risk_percent=risk_percent,
            equity=equity,
        )
        return json.dumps(result, default=str)

    @tool
    async def place_bracket_order(
        symbol: str,
        buy_trigger: float,
        sell_trigger: float,
        volume_lots: float,
        strategy_id: str = "autonomous_v1",
        sl_atr_multiplier: float = 1.5,
        tp_atr_multiplier: float = 3.0,
        rationale: str | None = None,
    ) -> str:
        """Place paired BUY STOP + SELL STOP bracket orders for breakout capture. SL/TP computed from ATR."""
        result = await client.place_bracket_order(
            symbol=symbol,
            buy_trigger=buy_trigger,
            sell_trigger=sell_trigger,
            volume_lots=volume_lots,
            strategy_id=strategy_id,
            sl_atr_multiplier=sl_atr_multiplier,
            tp_atr_multiplier=tp_atr_multiplier,
            rationale=rationale,
        )
        return json.dumps(result, default=str)

    @tool
    async def set_trailing_stop(
        position_id: str,
        distance_atr_multiplier: float = 1.5,
        lock_in_profit_after_atr: float = 2.0,
        check_interval_seconds: int = 30,
    ) -> str:
        """Start server-side trailing stop on a position. Auto-trails SL based on ATR."""
        result = await client.set_trailing_stop(
            position_id=position_id,
            distance_atr_multiplier=distance_atr_multiplier,
            lock_in_profit_after_atr=lock_in_profit_after_atr,
            check_interval_seconds=check_interval_seconds,
        )
        return json.dumps(result, default=str)

    @tool
    async def trail_position(
        position_id: str,
        distance_points: float,
        lock_in_points: float,
    ) -> str:
        """Manually trail stop on a position. distance_points: how far behind current price, lock_in_points: profit to lock."""
        result = await client.trail_position(
            position_id=position_id,
            distance_points=distance_points,
            lock_in_points=lock_in_points,
        )
        return json.dumps(result, default=str)

    @tool
    async def trailing_stop_list() -> str:
        """List all active trailing stops."""
        result = await client.trailing_stop_list()
        return json.dumps(result, default=str)

    @tool
    async def trailing_stop_cancel(position_id: str) -> str:
        """Cancel a server-side trailing stop for a position."""
        result = await client.trailing_stop_cancel(position_id)
        return json.dumps(result, default=str)

    @tool
    async def get_ticks(symbol: str, count: int = 50) -> str:
        """Fetch recent ticks for precise entry timing."""
        result = await client.get_ticks(symbol, count)
        return json.dumps(result, default=str)

    @tool
    async def symbol_info(symbol: str) -> str:
        """Get symbol metadata: point value, min/max volume, stop levels."""
        result = await client.symbol_info(symbol)
        return json.dumps(result, default=str)

    @tool
    async def get_order_book(symbol: str) -> str:
        """Fetch order book (DOM) snapshot. Shows bid/ask depth for liquidity analysis."""
        result = await client.get_order_book(symbol)
        return json.dumps(result, default=str)

    @tool
    async def multi_timeframe_indicators(
        symbol: str,
        indicator: str,
        timeframes: list[str],
        period: int = 14,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> str:
        """Get indicator across multiple timeframes at once. timeframes: e.g. ['M15','H1','H4','D1']."""
        result = await client.multi_timeframe_indicators(
            symbol,
            indicator,
            timeframes,
            period=period,
            fast=fast,
            slow=slow,
            signal=signal,
        )
        return json.dumps(result, default=str)

    @tool
    async def correlation_matrix(
        symbols: list[str],
        timeframe: str = "H1",
        lookback: int = 100,
    ) -> str:
        """Cross-symbol return correlation matrix. Identifies correlated exposure (e.g., long EURUSD + long GBPUSD = double USD risk)."""
        result = await client.correlation_matrix(
            symbols=symbols,
            timeframe=timeframe,
            lookback=lookback,
        )
        return json.dumps(result, default=str)

    @tool
    async def support_resistance(
        symbol: str,
        timeframe: str = "H1",
        lookback: int = 100,
    ) -> str:
        """Detect support and resistance levels from recent price action."""
        result = await client.support_resistance(
            symbol=symbol,
            timeframe=timeframe,
            lookback=lookback,
        )
        return json.dumps(result, default=str)

    @tool
    async def trading_insights(lookback_days: int = 7) -> str:
        """Auto-analyze past trading patterns: win rate by regime, emotion, common mistakes."""
        result = await client.trading_insights(lookback_days)
        return json.dumps(result, default=str)

    @tool
    async def trading_decision_support(symbol: str, side: str) -> str:
        """One-call decision support: regime + ATR + RSI + EMA20 + EMA50 + coaching in single batched round-trip (~400ms vs 3-5s sequential)."""
        result = await client.trading_decision_support(symbol, side)
        return json.dumps(result, default=str)

    return [
        market_regime,
        trading_context,
        trading_decision_support,
        get_bars,
        get_indicator,
        market_scan,
        trading_coach,
        trading_reflect,
        news_fetch,
        positions_open,
        account_summary,
        submit_market_order,
        close_position,
        volatility_profile,
        log_decision,
        submit_pending_order,
        orders_pending,
        cancel_order,
        cancel_all_orders,
        modify_order,
        modify_position_sl_tp,
        close_all_positions,
        validate_trade_setup,
        calculate_position_size,
        place_bracket_order,
        set_trailing_stop,
        trail_position,
        trailing_stop_list,
        trailing_stop_cancel,
        get_ticks,
        symbol_info,
        get_order_book,
        multi_timeframe_indicators,
        correlation_matrix,
        support_resistance,
        trading_insights,
    ]


def make_heartbeat_tools(heartbeat_engine) -> list:
    if heartbeat_engine is None:
        return []

    @tool
    async def get_recent_events(event_type: str | None = None, limit: int = 10) -> str:
        """Get recent market events (price alerts, volatility spikes, news)."""
        bus = heartbeat_engine.event_bus
        if event_type:
            from jesse.market_event_bus import EventType

            try:
                et = EventType[event_type.upper()]
            except KeyError:
                return f"Unknown event type: {event_type}. Valid: {', '.join(e.name for e in EventType)}"
            events = bus.get_recent_events(et, limit)
        else:
            events = bus.get_recent_events(None, limit)
        if not events:
            return "No recent events."
        lines = [
            f"[{e.timestamp:.0f}] {e.event_type.name}: {e.symbol} — {e.severity} | {e.data}"
            for e in events
        ]
        return "\n".join(lines)

    @tool
    async def add_price_alert(
        symbol: str,
        condition: str,
        price: float,
        severity: str = "medium",
        cooldown_seconds: int = 300,
    ) -> str:
        """Set a price alert. Conditions: above, below, crosses_up, crosses_down."""
        monitor = heartbeat_engine.price_monitor
        if not monitor:
            return "Price monitor not initialized."
        try:
            alert_id = await monitor.add_alert(
                symbol, condition, price, severity, cooldown_seconds
            )
            return f"Price alert set: {symbol} {condition} {price} (id={alert_id})"
        except ValueError as e:
            return f"Invalid alert: {e}"

    @tool
    async def remove_price_alert(alert_id: str) -> str:
        """Remove a previously set price alert."""
        monitor = heartbeat_engine.price_monitor
        if not monitor:
            return "Price monitor not initialized."
        ok = await monitor.remove_alert(alert_id)
        return (
            f"Price alert {alert_id} removed." if ok else f"Alert {alert_id} not found."
        )

    @tool
    async def list_price_alerts(symbol: str | None = None) -> str:
        """List all active price alerts."""
        monitor = heartbeat_engine.price_monitor
        if not monitor:
            return "Price monitor not initialized."
        alerts = await monitor.list_alerts(symbol)
        if not alerts:
            return "No active price alerts."
        lines = [
            f"{a.id}: {a.symbol} {a.condition} {a.price} ({a.severity}, cooldown={a.cooldown_seconds}s, triggered={a.triggered})"
            for a in alerts
        ]
        return "\n".join(lines)

    @tool
    async def get_volatility_states(symbols: list[str] | None = None) -> str:
        """Get current ATR-based volatility states for symbols. Shows if market is normal, spiking, or compressed (squeeze)."""
        monitor = heartbeat_engine.vol_monitor
        if not monitor:
            return "Volatility monitor not initialized."
        if symbols:
            states = {s: monitor.get_state(s) for s in symbols}
        else:
            states = monitor.get_all_states()
        if not states:
            return "No volatility data. Call get_volatility_states with specific symbols first."
        lines = []
        for sym, state in states.items():
            if state is None:
                lines.append(f"{sym}: no data")
            else:
                lines.append(
                    f"{sym}: regime={state.regime}, ATR={state.atr_current:.1f} (avg={state.atr_average:.1f}, percentile={state.atr_percentile:.0f}%)"
                )
        return "\n".join(lines)

    @tool
    async def get_upcoming_news(hours_ahead: float = 4.0) -> str:
        """Get upcoming high-impact news events."""
        monitor = heartbeat_engine.news_monitor
        if not monitor:
            return "News monitor not initialized."
        events = await monitor.get_upcoming_events(hours_ahead)
        if not events:
            return f"No high-impact news in the next {hours_ahead:.0f} hours."
        lines = []
        for e in events:
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(e.scheduled_time, tz=timezone.utc).strftime(
                "%H:%M UTC"
            )
            lines.append(f"[{dt}] {e.impact.upper()}: {e.title} → affects {e.symbol}")
        return "\n".join(lines)

    @tool
    async def get_heartbeat_context() -> str:
        """Get full proactivity context: active sessions, recent events, upcoming news, volatility, price alerts."""
        ctx = heartbeat_engine.get_context()
        parts = []
        if ctx.get("active_sessions"):
            parts.append(f"Active sessions: {', '.join(ctx['active_sessions'])}")
        if ctx.get("session_volatility_hint"):
            parts.append(f"Session volatility: {ctx['session_volatility_hint']}")
        if ctx.get("recent_events"):
            parts.append("Recent events:")
            parts.extend(f"  - {e}" for e in ctx["recent_events"][:10])
        if ctx.get("upcoming_news"):
            parts.append("Upcoming news:")
            parts.extend(f"  - {n}" for n in ctx["upcoming_news"][:3])
        if ctx.get("price_alerts"):
            parts.append(f"Active price alerts: {ctx['price_alerts']}")
        if ctx.get("volatility_states"):
            parts.append("Volatility:")
            for sym, state in ctx["volatility_states"].items():
                if state and hasattr(state, "regime"):
                    parts.append(
                        f"  {sym}: regime={state.regime}, ATR={state.atr_current:.1f} (avg={state.atr_average:.1f})"
                    )
                elif state and isinstance(state, str):
                    parts.append(f"  {sym}: {state}")
        if not parts:
            return "No active market events."
        return "\n".join(parts)

    return [
        get_recent_events,
        add_price_alert,
        remove_price_alert,
        list_price_alerts,
        get_volatility_states,
        get_upcoming_news,
        get_heartbeat_context,
    ]


def make_image_tools(mcp_client: Any = None, llm: Any = None) -> list:
    """Create LangChain tools for image/chart analysis."""
    from jesse.image_tools import make_image_tools as _make_image_tools

    return _make_image_tools(mcp_client, llm)

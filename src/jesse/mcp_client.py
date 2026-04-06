"""Autonomous 24/7 AI Trading Agent — MT5-MCP HTTP Client.

Async HTTP client that wraps all MT5-MCP server tool calls.
Connects to the MCP server on port 8010.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Retry Configuration (OpenCode-inspired) ──────────────────────────

# Default timeout: 30s per request (LLM calls can be slow)
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

MAX_RETRIES = 5
RETRY_INITIAL_DELAY = 1.0  # seconds
RETRY_BACKOFF_FACTOR = 2.0
RETRY_MAX_DELAY_NO_HEADERS = 30.0  # cap when no retry-after header
RETRY_MAX_DELAY = 120.0  # absolute cap
RETRY_JITTER = 0.1  # 10% random jitter

# Transient errors worth retrying (from OpenCode's retry.ts)
TRANSIENT_MESSAGES = [
    "load failed",
    "network connection was lost",
    "network request failed",
    "failed to fetch",
    "econnreset",
    "econnrefused",
    "etimedout",
    "socket hang up",
    "connection reset",
    "broken pipe",
    "temporary failure",
]

# Non-retryable HTTP status codes
NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 422}


def _is_transient_error(exc: Exception) -> bool:
    """Check if an error is transient and worth retrying."""
    message = str(exc).lower()
    return any(m in message for m in TRANSIENT_MESSAGES)


def _is_retryable_status(status: int) -> bool:
    """4xx client errors (except 429) are NOT retryable."""
    if status in NON_RETRYABLE_STATUSES:
        return False
    if status == 429:
        return True
    return status >= 500


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    """Extract retry-after delay from response headers."""
    retry_after_ms = headers.get("retry-after-ms")
    if retry_after_ms:
        try:
            return min(float(retry_after_ms) / 1000, RETRY_MAX_DELAY)
        except (ValueError, TypeError):
            pass

    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), RETRY_MAX_DELAY)
        except (ValueError, TypeError):
            try:
                delay = (
                    time.mktime(time.strptime(retry_after, "%a, %d %b %Y %H:%M:%S GMT"))
                    - time.time()
                )
                if delay > 0:
                    return min(delay, RETRY_MAX_DELAY)
            except (ValueError, OverflowError):
                pass
    return None


def _retry_delay(attempt: int, exc: Exception | None = None) -> float:
    """Exponential backoff with jitter, respecting retry-after headers."""
    if isinstance(exc, httpx.HTTPStatusError):
        header_delay = _parse_retry_after(exc.response.headers)
        if header_delay is not None:
            jitter = header_delay * RETRY_JITTER * random.uniform(-1, 1)
            return max(0.1, header_delay + jitter)

    base = RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    capped = min(base, RETRY_MAX_DELAY_NO_HEADERS)
    jitter = capped * RETRY_JITTER * random.uniform(0, 1)
    return capped + jitter


class MCPClient:
    """Async HTTP client for the MT5-MCP server.

    Usage:
        client = MCPClient(base_url="http://127.0.0.1:8010")
        bars = await client.get_bars("BTCUSD", "H1", 100)
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8010"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MCPClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── Internal helpers ──────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client.request(
                    method, path, json=json, params=params
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and "content" in data:
                    content = data["content"]
                    if isinstance(content, list) and len(content) > 0:
                        first = content[0]
                        if isinstance(first, dict) and "text" in first:
                            import json as _json

                            try:
                                return _json.loads(first["text"])
                            except (_json.JSONDecodeError, TypeError):
                                return first["text"]
                    return content if content else data
                return data
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if not _is_retryable_status(exc.response.status_code):
                    logger.error(
                        "Non-retryable HTTP error %d on %s %s: %s",
                        exc.response.status_code,
                        method,
                        path,
                        exc,
                    )
                    raise
                if attempt < MAX_RETRIES:
                    wait = _retry_delay(attempt, exc)
                    logger.warning(
                        "MCP %s %s failed (attempt %d/%d, status %d): %s — retrying in %.1fs",
                        method,
                        path,
                        attempt,
                        MAX_RETRIES,
                        exc.response.status_code,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "MCP %s %s failed after %d attempts (status %d): %s",
                        method,
                        path,
                        MAX_RETRIES,
                        exc.response.status_code,
                        exc,
                    )
                    raise
            except httpx.RequestError as exc:
                last_exc = exc
                if not _is_transient_error(exc):
                    logger.error(
                        "Non-transient request error on %s %s: %s",
                        method,
                        path,
                        exc,
                    )
                    raise
                if attempt < MAX_RETRIES:
                    wait = _retry_delay(attempt, exc)
                    logger.warning(
                        "MCP %s %s transient failure (attempt %d/%d): %s — retrying in %.1fs",
                        method,
                        path,
                        attempt,
                        MAX_RETRIES,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "MCP %s %s failed after %d attempts: %s",
                        method,
                        path,
                        MAX_RETRIES,
                        exc,
                    )
                    raise
        raise last_exc  # type: ignore[misc]

    async def _get(self, path: str, *, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, *, json: dict | None = None) -> dict:
        return await self._request("POST", path, json=json)

    # ── Market Data ───────────────────────────────────────────────────

    async def get_bars(self, symbol: str, timeframe: str, count: int = 100) -> dict:
        """Fetch OHLCV candles."""
        return await self._post(
            "/tools/get_bars",
            json={
                "symbol": symbol,
                "timeframe": timeframe,
                "count": count,
            },
        )

    async def get_indicator(
        self,
        symbol: str,
        timeframe: str,
        indicator: str,
        **kwargs: Any,
    ) -> dict:
        """Fetch indicator value(s). Pass period, fast, slow, etc. as kwargs."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "indicator": indicator,
        }
        payload.update(kwargs)
        return await self._post("/tools/get_indicator", json=payload)

    async def get_ticks(self, symbol: str, count: int = 50) -> dict:
        """Fetch recent ticks."""
        return await self._post(
            "/tools/get_ticks",
            json={
                "symbol": symbol,
                "count": count,
            },
        )

    async def symbol_info(self, symbol: str) -> dict:
        """Fetch symbol metadata (point value, min volume, stops level)."""
        return await self._post("/tools/get_symbol_info", json={"symbol": symbol})

    async def get_chart_screenshot(
        self, symbol: str, timeframe: str = "H1", width: int = 1920, height: int = 1080
    ) -> dict:
        """Fetch chart screenshot as base64 PNG."""
        return await self._post(
            "/tools/get_chart_screenshot",
            json={
                "symbol": symbol,
                "timeframe": timeframe,
                "width": width,
                "height": height,
            },
        )

    async def get_order_book(self, symbol: str) -> dict:
        """Fetch order book snapshot (DOM)."""
        return await self._post("/tools/get_order_book", json={"symbol": symbol})

    # ── Context & Analysis ────────────────────────────────────────────

    async def trading_coach(
        self,
        symbol: str,
        side: str,
        sl_distance_points: float | None = None,
        tp_distance_points: float | None = None,
        **kwargs: Any,
    ) -> dict:
        """Advisory feedback from live market data."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
        }
        if sl_distance_points is not None:
            payload["sl_distance_points"] = sl_distance_points
        if tp_distance_points is not None:
            payload["tp_distance_points"] = tp_distance_points
        payload.update(kwargs)
        return await self._post("/tools/trading/coach", json=payload)

    async def trading_context(self, symbol: str) -> dict:
        """Live context: ATR, volatility assessment, point values, composure notes."""
        return await self._post("/tools/trading/context", json={"symbol": symbol})

    async def trading_decision_support(self, symbol: str, side: str) -> dict:
        """One-call: regime + ATR + RSI + EMAs + coaching. Batched single round-trip."""
        return await self._post(
            "/tools/trading/decision_support",
            json={"symbol": symbol, "side": side},
        )

    async def market_regime(self, symbol: str, timeframe: str = "H1") -> dict:
        """Detect market regime: ranging, trending_up, trending_down, compressing."""
        return await self._post(
            "/tools/market/regime",
            json={
                "symbol": symbol,
                "timeframe": timeframe,
            },
        )

    async def market_scan(self, symbols: list[str], timeframe: str = "H1") -> dict:
        """Multi-symbol scan: price, ATR, regime for all symbols."""
        return await self._post(
            "/tools/market/scan",
            json={
                "symbols": symbols,
                "timeframe": timeframe,
            },
        )

    async def volatility_profile(
        self, symbol: str, timeframe: str = "H1", lookback: int = 20
    ) -> dict:
        """Summarize ATR and bar ranges."""
        return await self._post(
            "/tools/volatility_profile",
            json={
                "symbol": symbol,
                "timeframe": timeframe,
                "lookback": lookback,
            },
        )

    # ── Account & Positions ───────────────────────────────────────────

    async def account_summary(self) -> dict:
        """Get account summary (balance, equity, margin, etc.)."""
        return await self._get("/tools/get_account_summary")

    async def positions_open(self) -> list[dict]:
        """List open positions."""
        return await self._get("/tools/get_positions")

    async def orders_pending(self) -> list[dict]:
        """List pending orders."""
        return await self._get("/tools/get_orders")

    async def bridge_status(self) -> dict:
        """Bridge heartbeat status."""
        return await self._get("/resources/mt5/bridge/status")

    # ── Execution ─────────────────────────────────────────────────────

    async def submit_market_order(
        self,
        intent_id: str,
        strategy_id: str,
        account_id: str,
        symbol: str,
        side: str,
        order_kind: str,
        volume_lots: float,
        sl: float | None = None,
        tp: float | None = None,
        deviation_points: int | None = None,
    ) -> dict:
        """Submit market order via bridge."""
        payload: dict[str, Any] = {
            "intent_id": intent_id,
            "strategy_id": strategy_id,
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "order_kind": order_kind,
            "volume_lots": volume_lots,
        }
        if sl is not None:
            payload["sl"] = sl
        if tp is not None:
            payload["tp"] = tp
        if deviation_points is not None:
            payload["deviation_points"] = deviation_points
        return await self._post("/tools/submit_market_order_via_bridge", json=payload)

    async def submit_pending_order(
        self,
        symbol: str,
        side: str,
        kind: str,
        price: float,
        volume_lots: float,
        sl: float | None = None,
        tp: float | None = None,
        deviation: int | None = None,
    ) -> dict:
        """Submit pending (limit/stop) order."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "kind": kind,
            "price": price,
            "volume_lots": volume_lots,
        }
        if sl is not None:
            payload["sl"] = sl
        if tp is not None:
            payload["tp"] = tp
        if deviation is not None:
            payload["deviation"] = deviation
        return await self._post("/tools/submit_pending_order", json=payload)

    async def close_position(
        self, position_id: str, volume: float | None = None
    ) -> dict:
        """Close position (partial or full)."""
        payload: dict[str, Any] = {"position_id": position_id}
        if volume is not None:
            payload["volume"] = volume
        return await self._post("/tools/close_position", json=payload)

    async def modify_position_sl_tp(
        self,
        position_id: str,
        sl: float | None = None,
        tp: float | None = None,
    ) -> dict:
        """Modify position SL/TP."""
        payload: dict[str, Any] = {"position_id": position_id}
        if sl is not None:
            payload["sl"] = sl
        if tp is not None:
            payload["tp"] = tp
        return await self._post("/tools/modify_position_sl_tp", json=payload)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending order by ID."""
        return await self._post("/tools/cancel_order", json={"order_id": order_id})

    async def cancel_all_orders(
        self, symbol: str | None = None, side: str | None = None
    ) -> dict:
        """Cancel all pending orders, optionally filtered by symbol/side."""
        payload: dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        if side:
            payload["side"] = side
        return await self._post("/tools/cancel_all_orders", json=payload)

    async def modify_order(
        self,
        order_id: str,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> dict:
        """Modify a pending order's price, SL, or TP."""
        payload: dict[str, Any] = {"order_id": order_id}
        if price is not None:
            payload["new_price"] = price
        if sl is not None:
            payload["new_sl"] = sl
        if tp is not None:
            payload["new_tp"] = tp
        return await self._post("/tools/modify_order", json=payload)

    async def close_all_positions(
        self, symbol: str | None = None, side: str | None = None
    ) -> dict:
        """Close all positions, optionally filtered by symbol/side."""
        payload: dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        if side:
            payload["side"] = side
        return await self._post("/tools/close_all_positions", json=payload)

    # ── Validation & Sizing ───────────────────────────────────────────

    async def validate_trade_setup(
        self,
        symbol: str,
        side: str,
        order_kind: str,
        volume_lots: float,
        entry_price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> dict:
        """Validate trade against broker constraints."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "order_kind": order_kind,
            "volume_lots": volume_lots,
        }
        if entry_price is not None:
            payload["entry_price"] = entry_price
        if sl is not None:
            payload["sl"] = sl
        if tp is not None:
            payload["tp"] = tp
        return await self._post("/tools/validate_trade_setup", json=payload)

    async def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        risk_percent: float,
        equity: float | None = None,
    ) -> dict:
        """Calculate risk-based position size."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "risk_percent": risk_percent,
        }
        if equity is not None:
            payload["equity"] = equity
        return await self._post("/tools/calculate_position_size", json=payload)

    # ── Trailing Stops ────────────────────────────────────────────────

    async def trail_position(
        self, position_id: str, distance_points: float, lock_in_points: float
    ) -> dict:
        """Manual trail stop."""
        return await self._post(
            "/tools/trail_position",
            json={
                "position_id": position_id,
                "distance_points": distance_points,
                "lock_in_points": lock_in_points,
            },
        )

    async def set_trailing_stop(
        self,
        position_id: str,
        distance_atr_multiplier: float,
        check_interval_seconds: int = 10,
        lock_in_profit_after_atr: float | None = None,
    ) -> dict:
        """Start server-side trailing stop."""
        payload: dict[str, Any] = {
            "position_id": position_id,
            "distance_atr_multiplier": distance_atr_multiplier,
            "check_interval_seconds": check_interval_seconds,
        }
        if lock_in_profit_after_atr is not None:
            payload["lock_in_profit_after_atr"] = lock_in_profit_after_atr
        return await self._post("/tools/set_trailing_stop", json=payload)

    async def trailing_stop_list(self) -> dict:
        """List all active trailing stops."""
        return await self._post("/tools/trailing_stop/list")

    async def trailing_stop_cancel(self, position_id: str) -> dict:
        return await self._post(
            "/tools/trailing_stop/cancel",
            json={"position_id": position_id},
        )

    async def place_bracket_order(
        self,
        symbol: str,
        buy_trigger: float,
        sell_trigger: float,
        volume_lots: float,
        strategy_id: str = "autonomous_v1",
        sl_atr_multiplier: float = 1.5,
        tp_atr_multiplier: float = 3.0,
        rationale: str | None = None,
    ) -> dict:
        return await self._post(
            "/tools/place_bracket_order",
            json={
                "symbol": symbol,
                "buy_trigger": buy_trigger,
                "sell_trigger": sell_trigger,
                "volume_lots": volume_lots,
                "strategy_id": strategy_id,
                "sl_atr_multiplier": sl_atr_multiplier,
                "tp_atr_multiplier": tp_atr_multiplier,
                "rationale": rationale,
            },
        )

    async def multi_timeframe_indicators(
        self,
        symbol: str,
        indicator: str,
        timeframes: list[str],
        **kwargs: Any,
    ) -> dict:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "indicator": indicator,
            "timeframes": timeframes,
        }
        payload.update(kwargs)
        return await self._post("/tools/indicator/multi_timeframe", json=payload)

    async def correlation_matrix(
        self,
        symbols: list[str],
        timeframe: str = "H1",
        lookback: int = 100,
    ) -> dict:
        """Compute cross-symbol return correlation matrix."""
        return await self._post(
            "/tools/correlation_matrix",
            json={
                "symbols": symbols,
                "timeframe": timeframe,
                "lookback": lookback,
            },
        )

    async def support_resistance(
        self,
        symbol: str,
        timeframe: str = "H1",
        lookback: int = 100,
    ) -> dict:
        """Detect support and resistance levels from recent price action."""
        return await self._post(
            "/tools/support_resistance",
            json={
                "symbol": symbol,
                "timeframe": timeframe,
                "lookback": lookback,
            },
        )

    # ── Memory & Reflection ───────────────────────────────────────────

    async def trading_log_decision(
        self,
        symbol: str,
        side: str,
        action: str,
        model_justification: str | None = None,
        emotional_self_report: str | None = None,
        confidence_level: float | None = None,
        regime: str | None = None,
        rsi_value: float | None = None,
        atr_value: float | None = None,
        indicators_considered: list[str] | None = None,
        alternatives_considered: str | None = None,
        entry_price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        volume_lots: float | None = None,
        pnl: float | None = None,
        outcome: str | None = None,
        mistake_category: str | None = None,
        lesson_learned: str | None = None,
        decision_id: str | None = None,
        session_id: str | None = None,
        quality_rating: float | None = None,
        exit_price: float | None = None,
    ) -> dict:
        """Log a trading decision with reasoning."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "action": action,
        }
        if model_justification:
            payload["model_justification"] = model_justification
        if emotional_self_report:
            payload["emotional_self_report"] = emotional_self_report
        if confidence_level is not None:
            payload["confidence_level"] = confidence_level
        if regime:
            payload["regime"] = regime
        if rsi_value is not None:
            payload["rsi_value"] = rsi_value
        if atr_value is not None:
            payload["atr_value"] = atr_value
        if indicators_considered:
            payload["indicators_considered"] = indicators_considered
        if alternatives_considered:
            payload["alternatives_considered"] = alternatives_considered
        if entry_price is not None:
            payload["entry_price"] = entry_price
        if sl is not None:
            payload["sl"] = sl
        if tp is not None:
            payload["tp"] = tp
        if volume_lots is not None:
            payload["volume_lots"] = volume_lots
        if pnl is not None:
            payload["pnl"] = pnl
        if outcome:
            payload["outcome"] = outcome
        if mistake_category:
            payload["mistake_category"] = mistake_category
        if lesson_learned:
            payload["lesson_learned"] = lesson_learned
        if decision_id:
            payload["decision_id"] = decision_id
        if session_id:
            payload["session_id"] = session_id
        if quality_rating is not None:
            payload["quality_rating"] = quality_rating
        if exit_price is not None:
            payload["exit_price"] = exit_price
        return await self._post("/tools/trading/log_decision", json=payload)

    async def trading_reflect(
        self,
        symbol: str | None = None,
        action: str | None = None,
        regime: str | None = None,
        outcome: str | None = None,
        emotional_self_report: str | None = None,
        mistake_category: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Query past decisions for metacognitive reflection."""
        payload: dict[str, Any] = {"limit": limit}
        if symbol:
            payload["symbol"] = symbol
        if action:
            payload["action"] = action
        if regime:
            payload["regime"] = regime
        if outcome:
            payload["outcome"] = outcome
        if emotional_self_report:
            payload["emotional_self_report"] = emotional_self_report
        if mistake_category:
            payload["mistake_category"] = mistake_category
        return await self._post("/tools/trading/reflect", json=payload)

    async def trading_insights(self, lookback_days: int = 7) -> dict:
        """Auto-patterns from journal: win rate by emotion, regime, mistakes."""
        return await self._post(
            "/tools/trading/insights", json={"lookback_days": lookback_days}
        )

    # ── News ──────────────────────────────────────────────────────────

    async def news_fetch(
        self,
        pools: list[str] | None = None,
        limit: int = 10,
        keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        enrich_articles: bool = False,
    ) -> dict:
        """Fetch latest financial news."""
        payload: dict[str, Any] = {
            "limit": limit,
            "enrichArticles": enrich_articles,
        }
        if pools:
            payload["pools"] = pools
        if keywords:
            payload["keywords"] = keywords
        if exclude_keywords:
            payload["excludeKeywords"] = exclude_keywords
        return await self._post("/tools/news/fetch", json=payload)

    # ── Health ────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        """MCP server health check."""
        return await self._get("/health")

    async def performance_summary(self, days: int = 7, limit: int = 100) -> dict:
        return await self._get(
            "/resources/performance/summary", params={"days": days, "limit": limit}
        )

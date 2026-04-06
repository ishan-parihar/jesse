"""Telegram bot interface for the autonomous trading agent.

Supports bidirectional interaction: agent sends alerts/screenshots,
user sends commands to control the agent.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .conversation import ConversationManager
from .recovery import RecoveryTracker
from .telegram_format import FormattedChunk
from jesse.scheduler import get_active_symbols

logger = logging.getLogger(__name__)

_OFFSET_PATH = Path.home() / ".mt5-mcp" / "telegram_offset.json"
_SHUTDOWN_TS_PATH = Path.home() / ".mt5-mcp" / "shutdown-timestamp.json"

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}"

DEFAULT_CONVERSATION_TIMEOUT = 300.0  # 5 minutes

TELEGRAM_COMMANDS = [
    {"command": "start", "description": "Agent info"},
    {"command": "status", "description": "Agent + circuit breaker status"},
    {"command": "sleep", "description": "Pause trading cycles"},
    {"command": "wake", "description": "Resume trading cycles"},
    {"command": "chart", "description": "Send chart screenshots"},
    {"command": "positions", "description": "List open positions with PnL"},
    {"command": "orders", "description": "List pending orders"},
    {"command": "pnl", "description": "7-day performance summary"},
    {"command": "scan", "description": "Quick market scan"},
    {"command": "close", "description": "Close all positions"},
    {"command": "help", "description": "Command reference"},
]


def _build_inline_keyboard(rows: list[list[dict]]) -> dict | None:
    if not rows:
        return None
    keyboard = []
    for row in rows:
        keyboard.append(
            [
                {"text": b["text"], "callback_data": b["data"]}
                for b in row
                if b.get("text") and b.get("data")
            ]
        )
    return {"inline_keyboard": keyboard} if keyboard else None


class TelegramBot:
    """Bidirectional Telegram bot for agent interaction."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        scheduler: Any = None,
        mcp_client: Any = None,
        circuit_breaker: Any = None,
        jesse_agent: Any = None,
        conversation_mgr: Any = None,
        startup_info: dict | None = None,
        equity: float = 0.0,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.scheduler = scheduler
        self.mcp_client = mcp_client
        self.circuit_breaker = circuit_breaker
        self.jesse_agent = jesse_agent
        self.conversation_mgr = conversation_mgr
        self.startup_info = startup_info or {}
        self.equity = equity
        self._client = httpx.AsyncClient(
            base_url=TELEGRAM_API_URL.format(token=bot_token),
            timeout=30.0,
        )
        self._offset = self._load_offset()
        self._running = False
        self._command_handlers = self._register_handlers()
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._typing_circuit_broken = False
        self._commands_registered = False
        self.recovery_tracker = RecoveryTracker()

    async def _register_commands(self):
        if self._commands_registered:
            return
        try:
            await self._client.post(
                "/setMyCommands", json={"commands": TELEGRAM_COMMANDS}
            )
            self._commands_registered = True
            logger.info("Telegram command menu registered")
        except Exception as exc:
            logger.warning("Failed to register command menu: %s", exc)

    def _load_offset(self) -> int:
        try:
            data = json.loads(_OFFSET_PATH.read_text())
            return data.get("offset", 0)
        except (json.JSONDecodeError, FileNotFoundError):
            return 0

    def _save_offset(self):
        try:
            _OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
            _OFFSET_PATH.write_text(json.dumps({"offset": self._offset}))
        except Exception:
            logger.warning("Failed to save offset", exc_info=True)

    def _register_handlers(self) -> dict[str, Any]:
        return {
            "/start": self._cmd_start,
            "/status": self._cmd_status,
            "/sleep": self._cmd_sleep,
            "/wake": self._cmd_wake,
            "/chart": self._cmd_chart,
            "/positions": self._cmd_positions,
            "/orders": self._cmd_orders,
            "/pnl": self._cmd_pnl,
            "/scan": self._cmd_scan,
            "/close": self._cmd_close_all,
            "/help": self._cmd_help,
        }

    async def start(self):
        self._running = True
        logger.info("Telegram bot starting (polling)")
        await self._register_commands()
        try:
            await self._send_startup_message()
        except Exception:
            logger.warning("Startup message failed; bot will still poll")
        while self._running:
            try:
                await self._poll_updates()
            except Exception as exc:
                logger.error(
                    "Bot poll error: %s — %s", type(exc).__name__, exc, exc_info=True
                )
                await asyncio.sleep(3)

    async def _send_startup_message(self):
        startup = self.startup_info
        if not startup.get("is_restart"):
            await self.send("Autonomous Trading Agent online. Send /help for commands.")
            return

        dt = startup.get("downtime_seconds", 0)
        if dt < 60:
            downtime_text = f"{dt}s"
        elif dt < 3600:
            downtime_text = f"{dt // 60}m {dt % 60}s"
        else:
            downtime_text = f"{dt // 3600}h {(dt % 3600) // 60}m"

        reason = startup.get("reason", "unknown")
        reason_icon = "🔄" if reason in ("signal", "graceful") else "⚠️"

        parts = [
            f"{reason_icon} *Jesse back online* — downtime: {downtime_text}",
            "",
            f"Restart reason: {reason}",
            f"Equity: ${self.equity:.2f}",
        ]

        if self.circuit_breaker:
            ok, reason_text = self.circuit_breaker.check_all()
            parts.append(
                f"Circuit breaker: {'OK' if ok else f'TRIPPED — {reason_text}'}"
            )

        if self.mcp_client:
            try:
                positions = await self.mcp_client.positions_open()
                parts.append(f"Open positions: {len(positions) if positions else 0}")
            except Exception:
                parts.append("Open positions: unavailable")

        parts.append("")
        parts.append("Send /help for all commands.")

        await self.send("\n".join(parts))
        logger.info("Restart announcement sent")

        if self.startup_info.get("is_restart"):
            await self._recover_pending_messages()

    async def _recover_pending_messages(self):
        offset_age = self._offset_file_age_seconds()
        if offset_age < 0 or offset_age > 120:
            logger.info(
                "Checking for pending messages (offset age: %ds)",
                offset_age,
            )
            try:
                resp = await self._client.get(
                    "/getUpdates",
                    params={
                        "timeout": 5,
                        "allowed_updates": ["message"],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    return
                updates = data.get("result", [])
                pending = [
                    u
                    for u in updates
                    if u.get("message", {}).get("chat", {}).get("id")
                    == int(self.chat_id)
                    and u["update_id"] >= self._offset
                ]
                if pending:
                    logger.info(
                        "Found %d pending messages during downtime", len(pending)
                    )
                    for update in sorted(pending, key=lambda u: u["update_id"]):
                        msg = update.get("message")
                        if msg and msg.get("text"):
                            try:
                                await self._handle_message(msg)
                            except Exception as exc:
                                logger.error("Pending message error: %s", exc)
                        self._offset = update["update_id"] + 1
                    self._save_offset()
            except Exception as exc:
                logger.warning("Pending message recovery failed: %s", exc)

    def _offset_file_age_seconds(self) -> float:
        try:
            if _OFFSET_PATH.exists():
                mtime = _OFFSET_PATH.stat().st_mtime
                import time

                return time.time() - mtime
        except Exception:
            pass
        return -1

    async def stop(self):
        self._running = False
        await asyncio.sleep(0.5)
        await self._client.aclose()
        logger.info("Telegram bot stopped")

    async def _poll_updates(self):
        resp = await self._client.get(
            "/getUpdates",
            params={
                "offset": self._offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message")
            if msg:
                msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                if msg_chat_id != str(self.chat_id):
                    continue
                if not msg.get("text"):
                    await self.send("I only understand text commands. Send /help.")
                    continue
                try:
                    await self._handle_message(msg)
                except Exception as exc:
                    logger.error(
                        "Message handler error for update %d: %s",
                        update["update_id"],
                        exc,
                    )
                self._save_offset()
                continue

            cb = update.get("callback_query")
            if cb:
                try:
                    await self._handle_callback(cb)
                except Exception as exc:
                    logger.error(
                        "Callback handler error for update %d: %s",
                        update["update_id"],
                        exc,
                    )
                self._save_offset()

    async def _handle_callback(self, cb: dict):
        cb_id = cb.get("id", "")
        data = (cb.get("data") or "").strip()
        from_id = cb.get("from", {}).get("id", "")
        if str(from_id) != str(self.chat_id):
            await self._answer_callback(cb_id, text="Not authorized")
            return

        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", self.chat_id))
        msg_id = cb.get("message", {}).get("message_id")

        await self._answer_callback(cb_id)

        lock = self._get_chat_lock(chat_id)
        async with lock:
            if data == "refresh_status":
                await self._callback_refresh_status(chat_id, msg_id)
            elif data == "refresh_positions":
                await self._callback_refresh_positions(chat_id, msg_id)
            elif data == "refresh_orders":
                await self._callback_refresh_orders(chat_id, msg_id)
            elif data == "refresh_scan":
                await self._callback_refresh_scan(chat_id, msg_id)
            elif data.startswith("close_pos:"):
                pos_id = data.split(":", 1)[1]
                await self._callback_close_position(chat_id, msg_id, pos_id)
            elif data.startswith("cancel_ord:"):
                ord_id = data.split(":", 1)[1]
                await self._callback_cancel_order(chat_id, msg_id, ord_id)
            elif data == "close_all_confirm":
                await self._callback_close_all_positions(chat_id, msg_id)
            else:
                logger.debug("Unknown callback: %s", data)

    async def _answer_callback(self, cb_id: str, text: str = ""):
        try:
            params = {"callback_query_id": cb_id}
            if text:
                params["text"] = text
            await self._client.post("/answerCallbackQuery", json=params)
        except Exception:
            pass

    async def _edit_message_text(
        self,
        chat_id: str,
        msg_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup: dict | None = None,
    ):
        try:
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            if reply_markup:
                params["reply_markup"] = json.dumps(reply_markup)
            resp = await self._client.post("/editMessageText", json=params)
            if resp.status_code == 400:
                await self._edit_message_text_plain(chat_id, msg_id, text, reply_markup)
            return resp.status_code < 400
        except Exception as exc:
            logger.debug("Edit message failed: %s", exc)
            return False

    async def _edit_message_text_plain(
        self, chat_id: str, msg_id: int, text: str, reply_markup: dict | None = None
    ):
        try:
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": text,
            }
            if reply_markup:
                params["reply_markup"] = json.dumps(reply_markup)
            await self._client.post("/editMessageText", json=params)
        except Exception:
            pass

    async def _edit_message_reply_markup(
        self, chat_id: str, msg_id: int, reply_markup: dict
    ):
        try:
            await self._client.post(
                "/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "reply_markup": json.dumps(reply_markup),
                },
            )
        except Exception:
            pass

    async def _callback_refresh_status(self, chat_id: str, msg_id: int):
        await self.send_chat_action()
        if not self.scheduler:
            return
        interval = self.scheduler.current_interval
        running = self.scheduler._running
        status = "Running" if running else "Stopped"

        msg = (
            f"Agent Status\n\n"
            f"State: {status}\n"
            f"Check interval: {interval} min\n"
            f"Thread: trading_agent_main"
        )

        if self.circuit_breaker:
            cb = self.circuit_breaker.state
            ok, reason = self.circuit_breaker.check_all()
            status_icon = "OK" if ok else "TRIPPED"
            msg += (
                f"\n\nCircuit Breaker: {status_icon}\n"
                f"Consecutive losses: {cb.consecutive_losses}\n"
                f"Daily trades: {cb.daily_trades}\n"
                f"Daily loss: ${cb.daily_loss:.2f}"
            )
            if reason:
                msg += f"\n{reason}"

        kb = _build_inline_keyboard([[{"text": "Refresh", "data": "refresh_status"}]])
        await self._edit_message_text(chat_id, msg_id, msg, reply_markup=kb)

    async def _callback_refresh_positions(self, chat_id: str, msg_id: int):
        await self.send_chat_action()
        if not self.mcp_client:
            return
        try:
            positions = await self.mcp_client.positions_open()
            if not positions:
                kb = _build_inline_keyboard(
                    [[{"text": "Refresh", "data": "refresh_positions"}]]
                )
                await self._edit_message_text(
                    chat_id, msg_id, "No open positions.", reply_markup=kb
                )
                return

            msg = "Open Positions\n\n"
            rows = []
            for p in positions[:5]:
                sym = p.get("symbol") or "?"
                ptype = (p.get("type") or "?").upper()
                vol = p.get("volume", 0) or 0
                profit = p.get("profit", 0) or 0
                pos_id = str(p.get("id") or p.get("position_id") or "")
                msg += f"{sym} {ptype}\nVol: {vol} | PnL: ${profit:+.2f}\n\n"
                if pos_id:
                    rows.append(
                        [{"text": f"Close {sym}", "data": f"close_pos:{pos_id}"}]
                    )

            rows.append([{"text": "Close All", "data": "close_all_confirm"}])
            rows.append([{"text": "Refresh", "data": "refresh_positions"}])
            kb = _build_inline_keyboard(rows)
            await self._edit_message_text(chat_id, msg_id, msg, reply_markup=kb)
        except Exception as exc:
            await self._edit_message_text(chat_id, msg_id, f"Failed: {exc}")

    async def _callback_refresh_orders(self, chat_id: str, msg_id: int):
        await self.send_chat_action()
        if not self.mcp_client:
            return
        try:
            orders = await self.mcp_client.orders_pending()
            if not orders:
                kb = _build_inline_keyboard(
                    [[{"text": "Refresh", "data": "refresh_orders"}]]
                )
                await self._edit_message_text(
                    chat_id, msg_id, "No pending orders.", reply_markup=kb
                )
                return

            msg = "Pending Orders\n\n"
            rows = []
            for o in (orders if isinstance(orders, list) else orders.get("orders", []))[
                :5
            ]:
                sym = o.get("symbol") or "?"
                kind = (o.get("type") or o.get("order_kind") or "?").upper()
                price = o.get("price", 0) or 0
                ord_id = str(o.get("id") or o.get("order_id") or "")
                msg += f"{sym} {kind} @ {price}\n"
                if ord_id:
                    rows.append(
                        [{"text": f"Cancel {sym}", "data": f"cancel_ord:{ord_id}"}]
                    )

            rows.append([{"text": "Refresh", "data": "refresh_orders"}])
            kb = _build_inline_keyboard(rows)
            await self._edit_message_text(chat_id, msg_id, msg, reply_markup=kb)
        except Exception as exc:
            await self._edit_message_text(chat_id, msg_id, f"Failed: {exc}")

    async def _callback_refresh_scan(self, chat_id: str, msg_id: int):
        await self.send_chat_action()
        if not self.mcp_client:
            return
        try:
            symbols = get_active_symbols()
            scan = await self.mcp_client.market_scan(symbols=symbols, timeframe="H1")
            symbols_data = scan.get("symbols") or {}
            msg = "Market Scan (H1)\n\n"
            for sym in symbols:
                sym_data = symbols_data.get(sym, {})
                price = sym_data.get("bid", "?")
                atr = sym_data.get("atr", "?")
                regime = sym_data.get("regime", "unknown")
                rec = sym_data.get("recommendation", "")
                line = f"{sym} — ${price} | ATR: {atr} | {regime}"
                if rec:
                    line += f" | {rec}"
                msg += line + "\n"

            kb = _build_inline_keyboard([[{"text": "Refresh", "data": "refresh_scan"}]])
            await self._edit_message_text(chat_id, msg_id, msg, reply_markup=kb)
        except Exception as exc:
            await self._edit_message_text(chat_id, msg_id, f"Scan failed: {exc}")

    async def _callback_close_position(self, chat_id: str, msg_id: int, pos_id: str):
        await self.send_chat_action()
        if not self.mcp_client:
            return
        try:
            result = await self.mcp_client.close_position(pos_id)
            kb = _build_inline_keyboard(
                [[{"text": "Refresh", "data": "refresh_positions"}]]
            )
            await self._edit_message_text(
                chat_id, msg_id, f"Position closed: {result}", reply_markup=kb
            )
        except Exception as exc:
            await self._edit_message_text(chat_id, msg_id, f"Failed to close: {exc}")

    async def _callback_cancel_order(self, chat_id: str, msg_id: int, ord_id: str):
        await self.send_chat_action()
        if not self.mcp_client:
            return
        try:
            result = await self.mcp_client.cancel_order(ord_id)
            kb = _build_inline_keyboard(
                [[{"text": "Refresh", "data": "refresh_orders"}]]
            )
            await self._edit_message_text(
                chat_id, msg_id, f"Order cancelled: {result}", reply_markup=kb
            )
        except Exception as exc:
            await self._edit_message_text(chat_id, msg_id, f"Failed to cancel: {exc}")

    async def _callback_close_all_positions(self, chat_id: str, msg_id: int):
        await self.send_chat_action()
        if not self.mcp_client:
            return
        try:
            result = await self.mcp_client.close_all_positions()
            kb = _build_inline_keyboard(
                [[{"text": "Refresh", "data": "refresh_positions"}]]
            )
            await self._edit_message_text(
                chat_id, msg_id, f"All positions closed: {result}", reply_markup=kb
            )
        except Exception as exc:
            await self._edit_message_text(chat_id, msg_id, f"Failed: {exc}")

    async def _handle_message(self, msg: dict):
        text = msg.get("text", "").strip()
        if not text:
            return
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        handler = self._command_handlers.get(command)
        if handler:
            await handler(args)
            return

        if not self.jesse_agent or not self.conversation_mgr:
            logger.warning(
                "Conversation attempted but jesse_agent=%s, conversation_mgr=%s",
                self.jesse_agent is not None,
                self.conversation_mgr is not None,
            )
            await self.send(
                "Agent not initialized — conversational mode unavailable. "
                "Check server logs for startup errors."
            )
            return

        await self._handle_conversation(text, msg)

    async def _typing_refresher(self, done_event: asyncio.Event):
        while not done_event.is_set():
            await asyncio.sleep(5)
            if not done_event.is_set():
                await self.send_chat_action()

    async def _handle_conversation(self, text: str, msg: dict):
        chat_id = str(msg.get("chat", {}).get("id", self.chat_id))
        lock = self._get_chat_lock(chat_id)

        timeout = float(
            os.environ.get("JESSE_CONVERSATION_TIMEOUT", DEFAULT_CONVERSATION_TIMEOUT)
        )

        async with lock:
            done_event = asyncio.Event()
            typing_task = asyncio.create_task(self._typing_refresher(done_event))
            try:
                session = self.conversation_mgr.get_or_create_session(chat_id)
                context = self._build_context()

                result = await asyncio.wait_for(
                    self.jesse_agent.run_conversation(
                        user_message=text,
                        chat_id=chat_id,
                        context=context,
                    ),
                    timeout=timeout,
                )
                chunks = self.conversation_mgr.format_reply(result, session)
                if chunks:
                    await self.send_chunked(chunks)
            except asyncio.TimeoutError:
                logger.error("Conversation timed out for %s", chat_id)
                escalation = self.recovery_tracker.record(
                    "conversation_timeout", chat_id=chat_id
                )
                reply_text = (
                    escalation
                    or f"⏳ Request timed out ({timeout:.0f}s limit). The agent may be doing deep analysis. Try again or use /scan for a quick market overview."
                )
                try:
                    await self.send(reply_text, parse_mode="HTML")
                except Exception:
                    pass
            except Exception as exc:
                exc_type = type(exc).__name__
                logger.error("Conversation failed: %s", exc, exc_info=True)
                escalation = self.recovery_tracker.record(
                    "llm_error", chat_id=chat_id, details=str(exc)[:200]
                )
                reply_text = (
                    escalation
                    or f"Error processing your message ({exc_type}). Please try again."
                )
                try:
                    await self.send(reply_text, parse_mode="HTML")
                except Exception:
                    pass
            finally:
                done_event.set()
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

    def _build_context(self) -> dict:
        context = {}
        if self.circuit_breaker:
            ok, reason = self.circuit_breaker.check_all()
            context["circuit_breaker"] = (ok, reason)
        context["active_symbols"] = get_active_symbols()
        return context

    async def send(
        self, text: str, parse_mode: str = "Markdown", reply_markup: dict | None = None
    ) -> bool:
        try:
            params: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            if reply_markup:
                params["reply_markup"] = json.dumps(reply_markup)
            resp = await self._client.post("/sendMessage", json=params)
            resp.raise_for_status()
            return True
        except Exception as exc:
            if parse_mode and "parse mode" in str(exc).lower():
                try:
                    params: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
                    if reply_markup:
                        params["reply_markup"] = json.dumps(reply_markup)
                    resp = await self._client.post("/sendMessage", json=params)
                    resp.raise_for_status()
                    return True
                except Exception:
                    pass
            logger.error("Telegram send failed: %s", exc)
            return False

    async def send_photo(
        self, photo_bytes: bytes, caption: str = "", reply_markup: dict | None = None
    ) -> bool:
        try:
            data = {"chat_id": self.chat_id, "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            resp = await self._client.post(
                "/sendPhoto",
                data=data,
                files={"photo": ("chart.png", photo_bytes, "image/png")},
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram sendPhoto failed: %s", exc)
            return False

    async def send_chat_action(self, action: str = "typing") -> bool:
        if self._typing_circuit_broken:
            return False
        try:
            resp = await self._client.post(
                "/sendChatAction",
                json={"chat_id": self.chat_id, "action": action},
            )
            if resp.status_code == 401:
                self._typing_circuit_broken = True
                logger.warning("sendChatAction 401 — circuit breaker tripped")
                return False
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.debug("sendChatAction failed: %s", exc)
            return False

    async def send_chunked(
        self, chunks: list[FormattedChunk], delay: float = 1.0
    ) -> int:
        sent = 0
        for i, chunk in enumerate(chunks):
            try:
                if i > 0:
                    await asyncio.sleep(delay)
                ok = await self.send(chunk.text, parse_mode=chunk.parse_mode)
                if ok:
                    sent += 1
            except Exception as exc:
                logger.error("Chunk %d failed: %s", i, exc)
        return sent

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def send_chart_screenshot(self, symbol: str, timeframe: str = "H1") -> bool:
        if not self.mcp_client:
            return await self.send("MCP client not available for screenshot.")
        try:
            result = await self.mcp_client.get_chart_screenshot(
                symbol=symbol, timeframe=timeframe, width=1920, height=1080
            )
            import base64
            import json

            image_b64 = None

            if isinstance(result, dict):
                if "image_base64" in result:
                    image_b64 = result["image_base64"]
                elif "content" in result:
                    content = result["content"]
                    if isinstance(content, list) and content:
                        text_content = content[0].get("text", "{}")
                        try:
                            parsed = json.loads(text_content)
                            image_b64 = parsed.get("image_base64")
                        except json.JSONDecodeError:
                            pass

            if not image_b64:
                return await self.send(f"No screenshot data for {symbol} {timeframe}")

            photo = base64.b64decode(image_b64)
            caption = f"📊 *{symbol}* — {timeframe}\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
            return await self.send_photo(photo, caption)
        except Exception as exc:
            logger.error("Chart screenshot failed for %s: %s", symbol, exc)
            return await self.send(f"Failed to get chart for {symbol}: {exc}")

    # ── Command Handlers ──────────────────────────────────────────────

    async def _cmd_start(self, args):
        kb = _build_inline_keyboard(
            [
                [
                    {"text": "Status", "data": "refresh_status"},
                    {"text": "Positions", "data": "refresh_positions"},
                ],
                [
                    {"text": "Market Scan", "data": "refresh_scan"},
                    {"text": "Pending Orders", "data": "refresh_orders"},
                ],
            ]
        )
        await self.send(
            "Autonomous Trading Agent\n\n"
            "I monitor markets 24/7 and execute trades based on "
            "confluence scoring, regime detection, and learned patterns.\n\n"
            "Use the buttons below or type /help for all commands.",
            reply_markup=kb,
        )

    async def _cmd_status(self, args):
        if not self.scheduler:
            await self.send("Scheduler not available.")
            return
        interval = self.scheduler.current_interval
        running = self.scheduler._running
        status = "Running" if running else "Stopped"

        msg = (
            f"Agent Status\n\n"
            f"State: {status}\n"
            f"Check interval: {interval} min\n"
            f"Thread: trading_agent_main"
        )

        if self.circuit_breaker:
            cb = self.circuit_breaker.state
            ok, reason = self.circuit_breaker.check_all()
            status_icon = "OK" if ok else "TRIPPED"
            msg += (
                f"\n\nCircuit Breaker: {status_icon}\n"
                f"Consecutive losses: {cb.consecutive_losses}\n"
                f"Daily trades: {cb.daily_trades}\n"
                f"Daily loss: ${cb.daily_loss:.2f}"
            )
            if reason:
                msg += f"\n{reason}"

        kb = _build_inline_keyboard([[{"text": "Refresh", "data": "refresh_status"}]])
        await self.send(msg, reply_markup=kb)

    async def _cmd_sleep(self, args):
        if self.scheduler:
            self.scheduler.shutdown()
            await self.send("Agent put to sleep. Send /wake to resume.")
        else:
            await self.send("Scheduler not available.")

    async def _cmd_wake(self, args):
        if self.scheduler and not self.scheduler._running:
            self.scheduler.start()
            await self.send("Agent awakened. Resuming trading cycles.")
        else:
            await self.send("Agent is already running or scheduler unavailable.")

    async def _cmd_chart(self, args):
        symbols = args if args else get_active_symbols()
        tf = "H1"
        for sym in symbols[:3]:
            await self.send_chart_screenshot(sym.upper(), tf)
            await asyncio.sleep(1)

    async def _cmd_positions(self, args):
        if not self.mcp_client:
            await self.send("MCP client not available.")
            return
        await self.send_chat_action()
        try:
            positions = await self.mcp_client.positions_open()
            if not positions:
                kb = _build_inline_keyboard(
                    [[{"text": "Refresh", "data": "refresh_positions"}]]
                )
                await self.send("No open positions.", reply_markup=kb)
                return

            msg = "Open Positions\n\n"
            rows = []
            for p in positions[:5]:
                sym = p.get("symbol") or "?"
                ptype = (p.get("type") or "?").upper()
                vol = p.get("volume", 0) or 0
                profit = p.get("profit", 0) or 0
                sl = p.get("sl", 0) or 0
                tp = p.get("tp", 0) or 0
                pos_id = str(p.get("id") or p.get("position_id") or "")
                msg += (
                    f"{sym} {ptype}\n"
                    f"Vol: {vol} | PnL: ${profit:+.2f}\n"
                    f"SL: {sl} | TP: {tp}\n\n"
                )
                if pos_id:
                    rows.append(
                        [{"text": f"Close {sym}", "data": f"close_pos:{pos_id}"}]
                    )

            rows.append([{"text": "Close All", "data": "close_all_confirm"}])
            rows.append([{"text": "Refresh", "data": "refresh_positions"}])
            kb = _build_inline_keyboard(rows)
            await self.send(msg, reply_markup=kb)
        except Exception as exc:
            await self.send(f"Failed to fetch positions: {exc}")

    async def _cmd_orders(self, args):
        if not self.mcp_client:
            await self.send("MCP client not available.")
            return
        await self.send_chat_action()
        try:
            orders = await self.mcp_client.orders_pending()
            if not orders:
                kb = _build_inline_keyboard(
                    [[{"text": "Refresh", "data": "refresh_orders"}]]
                )
                await self.send("No pending orders.", reply_markup=kb)
                return

            order_list = (
                orders if isinstance(orders, list) else orders.get("orders", [])
            )
            if not order_list:
                kb = _build_inline_keyboard(
                    [[{"text": "Refresh", "data": "refresh_orders"}]]
                )
                await self.send("No pending orders.", reply_markup=kb)
                return

            msg = "Pending Orders\n\n"
            rows = []
            for o in order_list[:5]:
                sym = o.get("symbol") or "?"
                kind = (o.get("type") or o.get("order_kind") or "?").upper()
                price = o.get("price", 0) or 0
                ord_id = str(o.get("id") or o.get("order_id") or "")
                msg += f"{sym} {kind} @ {price}\n"
                if ord_id:
                    rows.append(
                        [{"text": f"Cancel {sym}", "data": f"cancel_ord:{ord_id}"}]
                    )

            rows.append([{"text": "Refresh", "data": "refresh_orders"}])
            kb = _build_inline_keyboard(rows)
            await self.send(msg, reply_markup=kb)
        except Exception as exc:
            await self.send(f"Failed to fetch orders: {exc}")

    async def _cmd_pnl(self, args):
        if not self.mcp_client:
            await self.send("MCP client not available.")
            return
        try:
            perf = await self.mcp_client.performance_summary(days=7) or {}
            msg = (
                f"Performance (7d)\n\n"
                f"Net PnL: ${perf.get('net_pnl', 0):+.2f}\n"
                f"Trades: {perf.get('total_trades', 0)}\n"
                f"Win Rate: {perf.get('win_rate', 0):.0%}\n"
                f"Best: ${perf.get('best_trade', 0):+.2f}\n"
                f"Worst: ${perf.get('worst_trade', 0):+.2f}"
            )
            await self.send(msg)
        except Exception as exc:
            await self.send(f"Failed to fetch performance: {exc}")

    async def _cmd_scan(self, args):
        if not self.mcp_client:
            await self.send("MCP client not available.")
            return
        await self.send_chat_action()
        symbols = args if args else get_active_symbols()
        try:
            scan = await self.mcp_client.market_scan(symbols=symbols, timeframe="H1")
            symbols_data = scan.get("symbols") or {}
            msg = f"Market Scan (H1)\n\n"
            for sym in symbols:
                sym_data = symbols_data.get(sym, {})
                price = sym_data.get("bid", "?")
                atr = sym_data.get("atr", "?")
                regime = sym_data.get("regime", "unknown")
                rec = sym_data.get("recommendation", "")
                line = f"{sym} — ${price} | ATR: {atr} | {regime}"
                if rec:
                    line += f" | {rec}"
                msg += line + "\n"

            kb = _build_inline_keyboard([[{"text": "Refresh", "data": "refresh_scan"}]])
            await self.send(msg, reply_markup=kb)
        except Exception as exc:
            await self.send(f"Scan failed: {exc}")

    async def _cmd_close_all(self, args):
        if not self.mcp_client:
            await self.send("MCP client not available.")
            return
        kb = _build_inline_keyboard(
            [
                [{"text": "Confirm Close All", "data": "close_all_confirm"}],
            ]
        )
        await self.send("Confirm: Close ALL open positions?", reply_markup=kb)

    async def _cmd_help(self, args):
        kb = _build_inline_keyboard(
            [
                [
                    {"text": "Status", "data": "refresh_status"},
                    {"text": "Positions", "data": "refresh_positions"},
                ],
                [
                    {"text": "Market Scan", "data": "refresh_scan"},
                    {"text": "Pending Orders", "data": "refresh_orders"},
                ],
            ]
        )
        await self.send(
            "Available Commands\n\n"
            "/start — Agent info\n"
            "/status — Agent + circuit breaker status\n"
            "/sleep — Pause trading cycles\n"
            "/wake — Resume trading cycles\n"
            "/chart [SYMBOLS...] — Send chart screenshots\n"
            "/positions — List open positions\n"
            "/orders — List pending orders\n"
            "/pnl — 7-day performance summary\n"
            "/scan [SYMBOLS...] — Quick market scan\n"
            "/close — Close all positions\n"
            "/help — This message\n\n"
            "Examples:\n"
            "/chart XAUUSD BTCUSD\n"
            "/scan ETHUSD\n\n"
            "You can also type natural language questions like:\n"
            '"What\'s the outlook on XAUUSD?"\n'
            '"Show me recent performance"',
            reply_markup=kb,
        )

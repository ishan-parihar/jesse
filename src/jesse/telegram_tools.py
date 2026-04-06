"""Telegram-specific agent tools.

Allows Jesse to send messages, manage conversations, and
interact with Telegram users as native tool calls.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def make_telegram_tools(bot: Any) -> list:
    """Create LangChain tools for Telegram interaction."""

    @tool
    async def telegram_send_message(text: str, chat_id: str = "") -> str:
        """Send a text message via Telegram. Use to notify users about trades, alerts, or responses."""
        try:
            target = chat_id or getattr(bot, "chat_id", "")
            if not target:
                return "Error: no chat_id available"
            await bot.send(text)
            return f"Message sent to {target}"
        except Exception as exc:
            return f"Failed to send message: {exc}"

    @tool
    async def telegram_send_photo(photo_bytes: str, caption: str = "") -> str:
        """Send a photo (base64-encoded) via Telegram. Use for charts and screenshots."""
        try:
            import base64

            data = base64.b64decode(photo_bytes)
            ok = await bot.send_photo(data, caption or "")
            return "Photo sent" if ok else "Failed to send photo"
        except Exception as exc:
            return f"Failed to send photo: {exc}"

    @tool
    async def telegram_send_alert(
        title: str, message: str, severity: str = "info"
    ) -> str:
        """Send a formatted alert via Telegram. severity: info, warning, critical."""
        icons = {
            "info": "\U0001f4e2",
            "warning": "\u26a0\ufe0f",
            "critical": "\U0001f6a8",
        }
        icon = icons.get(severity, "\U0001f4e2")
        text = f"{icon} <b>{title}</b>\n{message}"
        try:
            await bot.send(text, parse_mode="HTML")
            return f"Alert sent: {title}"
        except Exception as exc:
            return f"Failed to send alert: {exc}"

    return [telegram_send_message, telegram_send_photo, telegram_send_alert]

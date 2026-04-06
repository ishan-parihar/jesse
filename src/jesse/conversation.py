"""Conversation management for Jesse — thread IDs, session metadata, reply formatting."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .telegram_format import FormattedChunk, format_for_telegram

logger = logging.getLogger(__name__)


@dataclass
class SessionMeta:
    chat_id: str
    mode: str = "conversational"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    message_count: int = 0
    last_activity: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ConversationManager:
    """Tracks active conversation sessions and formats agent responses."""

    def __init__(self):
        self._sessions: dict[str, SessionMeta] = {}

    def get_or_create_session(self, chat_id: str) -> SessionMeta:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionMeta(chat_id=chat_id)
            logger.info("New conversation session: %s", chat_id)
        meta = self._sessions[chat_id]
        meta.message_count += 1
        meta.last_activity = datetime.now(timezone.utc).isoformat()
        return meta

    def get_thread_id(self, chat_id: str) -> str:
        return f"telegram:{chat_id}"

    def format_reply(
        self, agent_result: dict, session: SessionMeta | None = None
    ) -> list[FormattedChunk]:
        """Format agent response into Telegram-safe HTML chunks.

        Returns a list of FormattedChunk objects (openclaw pattern).
        Each chunk respects Telegram's 4096-char limit with HTML tag integrity.
        """
        text = agent_result.get("text", "")
        if not text or text == "No response generated":
            return [
                FormattedChunk(
                    text="I'm processing your request. Please try again.",
                    parse_mode="HTML",
                )
            ]
        return format_for_telegram(text)

    def get_active_sessions(self) -> list[SessionMeta]:
        return list(self._sessions.values())

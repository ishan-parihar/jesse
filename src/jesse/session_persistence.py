"""
SessionPersistence — JSONL-based conversation transcript storage.

Persists each turn of a conversation to disk so it survives restarts.
Inspired by openclaw's file-backed JSONL session transcripts.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionPersistence:
    """Appends conversation turns to a JSONL file per chat_id."""

    def __init__(self, data_dir: str | Path | None = None):
        self._data_dir = (
            Path(data_dir) if data_dir else Path.home() / ".mt5-mcp" / "conversations"
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def append_turn(self, chat_id: str, messages: list[dict]) -> None:
        """
        Append a list of messages to the chat's JSONL transcript.
        Each message should be a dict with: role, content, timestamp, optional tool_call_id.
        """
        filepath = self._data_dir / f"{chat_id}.jsonl"
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error(
                "SessionPersistence: failed to append turn for %s: %s", chat_id, exc
            )

    def load_transcript(self, chat_id: str, max_messages: int = 50) -> list[dict]:
        """Load the most recent max_messages from a chat's transcript."""
        filepath = self._data_dir / f"{chat_id}.jsonl"
        if not filepath.exists():
            return []
        try:
            lines = filepath.read_text(encoding="utf-8").strip().split("\n")
            lines = [l for l in lines if l.strip()]
            # Take the last max_messages
            recent = lines[-max_messages:]
            messages = []
            for line in recent:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return messages
        except Exception as exc:
            logger.error(
                "SessionPersistence: failed to load transcript for %s: %s", chat_id, exc
            )
            return []

    def clear_transcript(self, chat_id: str) -> None:
        """Clear a chat's transcript (e.g., on /reset)."""
        filepath = self._data_dir / f"{chat_id}.jsonl"
        try:
            if filepath.exists():
                filepath.unlink()
        except Exception as exc:
            logger.error(
                "SessionPersistence: failed to clear transcript for %s: %s",
                chat_id,
                exc,
            )

    def get_transcript_path(self, chat_id: str) -> Path:
        return self._data_dir / f"{chat_id}.jsonl"

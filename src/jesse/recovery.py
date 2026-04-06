"""Recovery recipes for Jesse — automatic failure detection, response, and escalation."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PERSIST_PATH = Path.home() / ".mt5-mcp" / "recovery-state.json"


@dataclass
class RecoveryEvent:
    recipe_name: str
    timestamp: float
    chat_id: str = ""
    details: str = ""


@dataclass
class Recipe:
    name: str
    trigger_count: int  # N events within window triggers
    trigger_window_seconds: float  # time window
    auto_action: str  # description of what to do automatically
    escalation_message: str  # user-facing message when escalated


# Recovery recipe definitions
RECOVERY_RECIPES: dict[str, Recipe] = {
    "conversation_timeout": Recipe(
        name="conversation_timeout",
        trigger_count=3,
        trigger_window_seconds=600,  # 10 minutes
        auto_action="suggest /scan instead of deep conversation",
        escalation_message="⏳ Deep analysis is struggling. Try /scan for a quick market overview, or ask a more specific question.",
    ),
    "llm_error": Recipe(
        name="llm_error",
        trigger_count=5,
        trigger_window_seconds=300,  # 5 minutes
        auto_action="attempt model failover (if configured)",
        escalation_message="⚠️ LLM provider may be unavailable. The agent is having trouble connecting. Please try again later.",
    ),
    "mcp_bridge_failure": Recipe(
        name="mcp_bridge_failure",
        trigger_count=3,
        trigger_window_seconds=300,  # 5 minutes
        auto_action="health check MT5 bridge",
        escalation_message="⚠️ MT5 bridge may be down. Check that the MetaTrader 5 terminal and bridge EA are running.",
    ),
}


class RecoveryTracker:
    """Tracks failure events and triggers recovery actions when thresholds are met."""

    def __init__(self, persist_path: Path = PERSIST_PATH):
        self._events: list[RecoveryEvent] = []
        self._persist_path = persist_path
        self._load()

    def _load(self):
        try:
            if self._persist_path.exists():
                data = json.loads(self._persist_path.read_text())
                self._events = [RecoveryEvent(**e) for e in data.get("events", [])]
                # Purge stale events older than max window
                max_window = max(
                    r.trigger_window_seconds for r in RECOVERY_RECIPES.values()
                )
                cutoff = time.time() - max_window - 60
                self._events = [e for e in self._events if e.timestamp > cutoff]
        except Exception as e:
            logger.warning("Failed to load recovery state: %s", e)
            self._events = []

    def _save(self):
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "recipe_name": e.recipe_name,
                                "timestamp": e.timestamp,
                                "chat_id": e.chat_id,
                                "details": e.details,
                            }
                            for e in self._events
                        ]
                    }
                )
            )
            tmp.rename(self._persist_path)
        except Exception as e:
            logger.warning("Failed to save recovery state: %s", e)

    def record(
        self, recipe_name: str, chat_id: str = "", details: str = ""
    ) -> str | None:
        """Record a failure event. Returns escalation message if threshold exceeded, else None."""
        event = RecoveryEvent(
            recipe_name=recipe_name,
            timestamp=time.time(),
            chat_id=chat_id,
            details=details,
        )
        self._events.append(event)
        self._save()

        recipe = RECOVERY_RECIPES.get(recipe_name)
        if not recipe:
            logger.warning("Unknown recovery recipe: %s", recipe_name)
            return None

        # Count recent events for this recipe
        cutoff = time.time() - recipe.trigger_window_seconds
        recent = [
            e
            for e in self._events
            if e.recipe_name == recipe_name and e.timestamp > cutoff
        ]

        if len(recent) >= recipe.trigger_count:
            logger.warning(
                "Recovery threshold exceeded: %d/%d events for '%s' in %.0fs — escalating",
                len(recent),
                recipe.trigger_count,
                recipe_name,
                recipe.trigger_window_seconds,
            )
            # Purge events for this recipe to prevent repeated escalation
            self._events = [e for e in self._events if e.recipe_name != recipe_name]
            self._save()
            return recipe.escalation_message

        return None

    def get_recent_events(
        self, recipe_name: str, window_seconds: float = 300
    ) -> list[RecoveryEvent]:
        """Get recent events for a recipe."""
        cutoff = time.time() - window_seconds
        return [
            e
            for e in self._events
            if e.recipe_name == recipe_name and e.timestamp > cutoff
        ]

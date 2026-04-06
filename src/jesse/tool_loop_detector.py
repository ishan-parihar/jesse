"""
ToolLoopDetector — Catches pathological tool call patterns in the ReAct loop.

Inspired by openclaw's 4-detector system. Implements 3 detectors relevant to trading:
1. generic_repeat: Same tool called with same args repeatedly
2. ping_pong: Two tools alternating (A→B→A→B...)
3. no_progress: N turns without a trade decision or meaningful action
"""

import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LoopDetectionConfig:
    warning_threshold: int = 5  # Warn after N repeats
    critical_threshold: int = 8  # Block after N repeats
    ping_pong_threshold: int = 6  # Detect alternating after N pairs
    no_progress_threshold: int = 6  # Warn after N turns without progress
    history_size: int = 20  # How many tool calls to track


class ToolLoopDetector:
    """Detects and prevents tool call loops in the ReAct agent."""

    def __init__(self, config: LoopDetectionConfig | None = None):
        self.config = config or LoopDetectionConfig()
        self._call_history: deque[dict] = deque(maxlen=self.config.history_size)
        self._turn_count = 0
        self._turns_without_progress = 0
        self._warnings_issued: set[str] = set()

    def record_turn(self, tool_calls: list[dict]) -> list[str]:
        """
        Record tool calls for this turn. Returns list of warnings if loops detected.
        Call this at the start of each ReAct turn, BEFORE executing tools.
        """
        self._turn_count += 1
        warnings = []

        for tc in tool_calls:
            call_entry = {
                "tool": tc.get("name", ""),
                "args_hash": self._hash_args(tc.get("arguments", {})),
                "turn": self._turn_count,
            }
            self._call_history.append(call_entry)

        # Run detectors
        warnings.extend(self._detect_generic_repeat())
        warnings.extend(self._detect_ping_pong())
        warnings.extend(self._detect_no_progress(tool_calls))

        return warnings

    def mark_progress(self):
        """Call this when the agent makes meaningful progress (trade decision, analysis complete)."""
        self._turns_without_progress = 0

    def mark_no_progress(self):
        """Call this when a turn produced no meaningful action."""
        self._turns_without_progress += 1

    def get_stats(self) -> dict:
        return {
            "total_turns": self._turn_count,
            "call_history_size": len(self._call_history),
            "turns_without_progress": self._turns_without_progress,
        }

    def reset(self):
        """Reset all state (called at start of new cycle)."""
        self._call_history.clear()
        self._turn_count = 0
        self._turns_without_progress = 0
        self._warnings_issued.clear()

    # ── Detector 1: Generic Repeat ──────────────────────────────────

    def _detect_generic_repeat(self) -> list[str]:
        """Detect same tool+args called repeatedly."""
        if len(self._call_history) < self.config.warning_threshold:
            return []

        recent = list(self._call_history)[-self.config.warning_threshold :]
        # Check if all recent calls are the same tool+args
        first = recent[0]
        if all(
            c["tool"] == first["tool"] and c["args_hash"] == first["args_hash"]
            for c in recent
        ):
            tool_name = first["tool"]
            count = len(recent)
            key = f"repeat:{tool_name}:{first['args_hash']}"

            if count >= self.config.critical_threshold:
                return [
                    f"CRITICAL: Tool '{tool_name}' called {count} times with same args — possible loop. Consider stopping."
                ]
            elif key not in self._warnings_issued:
                self._warnings_issued.add(key)
                return [
                    f"WARNING: Tool '{tool_name}' called {count} times with same args — check for loop."
                ]

        return []

    # ── Detector 2: Ping-Pong ──────────────────────────────────────

    def _detect_ping_pong(self) -> list[str]:
        """Detect two tools alternating: A→B→A→B→A→B..."""
        threshold = self.config.ping_pong_threshold
        if len(self._call_history) < threshold * 2:
            return []

        recent = list(self._call_history)[-threshold * 2 :]
        # Check alternating pattern
        tools = [c["tool"] for c in recent]
        if len(set(tools)) == 2:
            # Check if they alternate
            is_alternating = True
            for i in range(1, len(tools)):
                if tools[i] == tools[i - 1]:
                    is_alternating = False
                    break

            if is_alternating:
                key = f"pingpong:{tools[0]}:{tools[1]}"
                if key not in self._warnings_issued:
                    self._warnings_issued.add(key)
                    return [
                        f"WARNING: Ping-pong pattern detected: '{tools[0]}' ↔ '{tools[1]}' alternating for {threshold} turns."
                    ]

        return []

    # ── Detector 3: No Progress ─────────────────────────────────────

    def _detect_no_progress(self, tool_calls: list[dict]) -> list[str]:
        """Detect many turns without meaningful progress."""
        if self._turns_without_progress < self.config.no_progress_threshold:
            return []

        key = f"noprogress:{self._turns_without_progress}"
        if key not in self._warnings_issued:
            self._warnings_issued.add(key)
            return [
                f"WARNING: {self._turns_without_progress} turns without progress — consider returning a final answer."
            ]

        return []

    @staticmethod
    def _hash_args(args: dict) -> str:
        """Create a hash of tool arguments for comparison."""
        return hashlib.md5(str(sorted(args.items())).encode()).hexdigest()[:8]

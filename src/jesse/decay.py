"""Memory decay — importance-weighted forgetting for stale trading patterns."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jesse.semantic_memory import SemanticMemory

logger = logging.getLogger(__name__)

DECAY_RATE = 0.1
PRUNE_THRESHOLD = 0.1

IMPORTANCE_MULTIPLIERS = {
    "large_win": 3.0,
    "large_loss": 3.0,
    "regime_change": 2.0,
    "tilt_detected": 2.5,
    "default": 1.0,
}

RETENTION_DAYS = {
    "trade_episodes": 90,
    "semantic_rules": 365,
    "emotional_patterns": 30,
    "regime_stats": 60,
}


def compute_importance(metadata: dict[str, Any]) -> float:
    pnl = metadata.get("pnl", 0)
    trade_count = metadata.get("trade_count", 0)
    pattern_type = metadata.get("type", "")
    confidence = metadata.get("confidence", 0.5)

    base = confidence

    if abs(pnl) > 5.0:
        base *= IMPORTANCE_MULTIPLIERS["large_win" if pnl > 0 else "large_loss"]
    if trade_count >= 20:
        base *= 1.5
    if pattern_type == "emotion_warning":
        base *= IMPORTANCE_MULTIPLIERS["tilt_detected"]
    if pattern_type == "regime_warning":
        base *= IMPORTANCE_MULTIPLIERS["regime_change"]

    return min(base, 5.0)


def decay_strength(importance: float, hours_old: float) -> float:
    adjusted_rate = DECAY_RATE / (1.0 + importance)
    return importance * math.exp(-adjusted_rate * hours_old / 24.0)


def should_prune(current_strength: float) -> bool:
    return current_strength < PRUNE_THRESHOLD


def apply_decay(
    memory: SemanticMemory,
    max_age_days: int | None = None,
) -> dict[str, int]:
    if max_age_days is None:
        max_age_days = RETENTION_DAYS["semantic_rules"]

    results = {"pruned": 0, "updated": 0, "kept": 0}
    now = datetime.now(timezone.utc)

    all_items = memory.get_all()
    if not all_items:
        return results

    for item in all_items:
        doc_id = item["id"]
        meta = item["metadata"]
        created_str = item.get("created_at", "") or meta.get("created_at", "")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = (now - created).total_seconds() / 86400
            except (ValueError, TypeError):
                age_days = max_age_days + 1
        else:
            age_days = max_age_days + 1

        if age_days > max_age_days:
            memory.delete_by_id(doc_id)
            results["pruned"] += 1
            continue

        importance = compute_importance(meta)
        hours_old = age_days * 24
        current_strength = decay_strength(importance, hours_old)

        if should_prune(current_strength):
            memory.delete_by_id(doc_id)
            results["pruned"] += 1
        else:
            try:
                memory.update_metadata(
                    doc_id, {**meta, "current_strength": round(current_strength, 4)}
                )
                results["updated"] += 1
            except Exception:
                results["kept"] += 1

    if results["pruned"] > 0:
        logger.info(
            "Decay complete: pruned=%d, updated=%d, kept=%d",
            results["pruned"],
            results["updated"],
            results["kept"],
        )
    return results

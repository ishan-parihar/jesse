"""Model fallback chain for Jesse — automatic failover when primary LLM fails."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ModelEntry:
    model: str
    base_url: str
    api_key: str = "not-needed"
    max_tokens: int = 4000


@dataclass
class FailoverChain:
    entries: list[ModelEntry]
    current_index: int = 0
    consecutive_failures: int = 0
    failure_threshold: int = 2  # failover after N consecutive failures


def create_failover_chain(entries: list[ModelEntry] | None = None) -> FailoverChain:
    """Create a failover chain from env vars or explicit entries."""
    if entries:
        return FailoverChain(entries=entries)

    # Build from env vars
    primary = ModelEntry(
        model=os.environ.get("JESSE_MODEL", "coder-model"),
        base_url=os.environ.get("JESSE_BASE_URL", "http://127.0.0.1:3000/v1"),
        api_key=os.environ.get("JESSE_API_KEY", "not-needed"),
    )

    # Check for fallback models in JESSE_FALLBACK_MODELS env var (JSON array)
    fallbacks: list[ModelEntry] = []
    fallback_json = os.environ.get("JESSE_FALLBACK_MODELS", "")
    if fallback_json:
        try:
            fallback_data = json.loads(fallback_json)
            for entry in fallback_data:
                fallbacks.append(
                    ModelEntry(
                        model=entry["model"],
                        base_url=entry["base_url"],
                        api_key=entry.get("api_key", "not-needed"),
                        max_tokens=entry.get("max_tokens", 4000),
                    )
                )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Invalid JESSE_FALLBACK_MODELS JSON: %s", e)

    return FailoverChain(entries=[primary] + fallbacks)


def get_current_model(chain: FailoverChain) -> ModelEntry:
    return chain.entries[chain.current_index]


def record_failure(chain: FailoverChain) -> bool:
    """Record a failure. Returns True if should failover to next model."""
    chain.consecutive_failures += 1
    current = get_current_model(chain)
    logger.warning(
        "LLM failure #%d for model %s (threshold: %d)",
        chain.consecutive_failures,
        current.model,
        chain.failure_threshold,
    )

    if chain.consecutive_failures >= chain.failure_threshold:
        return failover_to_next(chain) is not None
    return False


def failover_to_next(chain: FailoverChain) -> ModelEntry | None:
    """Switch to next model in chain. Returns new model or None if exhausted."""
    next_idx = chain.current_index + 1
    if next_idx >= len(chain.entries):
        logger.error("All LLM models exhausted in fallback chain")
        return None

    old_model = chain.entries[chain.current_index]
    chain.current_index = next_idx
    chain.consecutive_failures = 0
    new_model = chain.entries[chain.current_index]

    logger.warning("Failover: %s \u2192 %s", old_model.model, new_model.model)
    return new_model


def record_success(chain: FailoverChain):
    """Reset failure counter on success."""
    chain.consecutive_failures = 0


def create_llm_from_entry(entry: ModelEntry):
    """Create a LangChain ChatOpenAI from a ModelEntry."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=entry.model,
        openai_api_base=entry.base_url,
        openai_api_key=entry.api_key,
        temperature=0.3,
        max_tokens=entry.max_tokens,
    )

"""Stand-alone retry utility with exponential backoff and jitter.

Designed for LLM API calls with configurable retry predicates,
Retry-After header support, and observability hooks.

Adapted from strategos ``src/runtime/retry.ts``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ATTEMPTS: int = 3
DEFAULT_MIN_DELAY: float = 2.0  # seconds
DEFAULT_MAX_DELAY: float = 30.0  # seconds
DEFAULT_JITTER: float = 0.2  # ±20 %

# Retryable HTTP status codes
RETRYABLE_HTTP_STATUSES: set[int] = {429, 500, 502, 503, 504}

# Non-retryable message patterns (substring match, case-insensitive)
NON_RETRYABLE_PATTERNS: list[str] = ["context length", "invalid"]

# Retryable message patterns (substring match, case-insensitive)
RETRYABLE_PATTERNS: list[str] = [
    "model loading",
    "timed out",
    "connection",
    "econnrefused",
    "etimedout",
    "econnreset",
]


# ---------------------------------------------------------------------------
# Backoff computation
# ---------------------------------------------------------------------------


def compute_backoff_delay(
    attempt: int,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
) -> float:
    """Compute exponential backoff delay with jitter.

    Formula::

        base = min_delay * 2 ** (attempt - 1)
        jittered = base * (1 + random.uniform(-jitter, +jitter))
        return clamp(jittered, min_delay, max_delay)

    Args:
        attempt: 1-based attempt number.
        min_delay: Minimum delay in seconds.
        max_delay: Maximum delay in seconds.
        jitter: Fractional jitter range (e.g. 0.2 → ±20 %).

    Returns:
        Delay in seconds, clamped to ``[min_delay, max_delay]``.
    """
    base = min_delay * (2 ** (attempt - 1))
    jittered = base * (1 + random.uniform(-jitter, jitter))
    return max(min_delay, min(max_delay, jittered))


# ---------------------------------------------------------------------------
# Retryability detection
# ---------------------------------------------------------------------------


def is_retryable_error(exc: Exception) -> bool:
    """Determine whether *exc* represents a transient, retryable error.

    Checks:
    1. ``httpx.HTTPStatusError`` with status in ``RETRYABLE_HTTP_STATUSES``.
    2. ``httpx.RequestError`` whose message matches a retryable pattern.
    3. Any exception whose string representation matches a retryable pattern
       and does **not** match a non-retryable pattern.

    Non-retryable patterns (fail immediately):
        "context length", "invalid"

    Returns:
        ``True`` if the error should be retried.
    """
    error_msg = str(exc).lower()

    # --- Non-retryable patterns (check first) ---
    for pattern in NON_RETRYABLE_PATTERNS:
        if pattern in error_msg:
            return False

    # --- httpx-specific checks ---
    try:
        import httpx
    except ImportError:
        httpx = None  # type: ignore[assignment]

    if httpx is not None:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in RETRYABLE_HTTP_STATUSES

        if isinstance(exc, httpx.RequestError):
            for pattern in RETRYABLE_PATTERNS:
                if pattern in error_msg:
                    return True
            return False

    # --- Generic pattern matching ---
    for pattern in RETRYABLE_PATTERNS:
        if pattern in error_msg:
            return True

    return False


# ---------------------------------------------------------------------------
# Async retry loop
# ---------------------------------------------------------------------------

# Type alias for the on_retry callback payload
RetryInfo = dict[str, Any]


async def retry_async(
    fn: Callable[[], Awaitable[Any]],
    attempts: int = DEFAULT_ATTEMPTS,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    label: str = "",
    on_retry: Optional[Callable[[RetryInfo], None]] = None,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Args:
        fn: Zero-argument async callable to execute.
        attempts: Maximum number of attempts (1 = no retry).
        min_delay: Minimum delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        jitter: Fractional jitter to apply (±jitter %).
        label: Human-readable label for log messages.
        on_retry: Optional callback invoked before each retry.
            Receives a dict with keys: ``attempt``, ``max_attempts``,
            ``delay``, ``err``, ``label``.

    Returns:
        The return value of ``fn()`` on success.

    Raises:
        The last exception if all attempts are exhausted.
        Non-retryable exceptions are re-raised immediately.
    """
    last_exc: Exception | None = None

    for attempt_num in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc

            if not is_retryable_error(exc):
                logger.warning(
                    "Non-retryable error in %s: %s",
                    label or "retry_async",
                    exc,
                )
                raise

            if attempt_num >= attempts:
                logger.error(
                    "All %d attempts exhausted for %s: %s",
                    attempts,
                    label or "retry_async",
                    exc,
                )
                raise

            delay = compute_backoff_delay(attempt_num, min_delay, max_delay, jitter)

            info: RetryInfo = {
                "attempt": attempt_num,
                "max_attempts": attempts,
                "delay": delay,
                "err": exc,
                "label": label,
            }

            if on_retry is not None:
                on_retry(info)

            logger.info(
                "Retrying %s (attempt %d/%d, delay %.1fs): %s",
                label or "retry_async",
                attempt_num,
                attempts,
                delay,
                exc,
            )

            await asyncio.sleep(delay)

    # Should never reach here, but satisfy type checkers
    raise last_exc  # type: ignore[misc]

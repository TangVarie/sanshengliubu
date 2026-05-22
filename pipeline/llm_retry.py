"""Bounded exponential-backoff retry wrapper for LLM calls.

Problem (2026-05-22 audit R-026): sanshengliubu's Gemini calls had no retry
on transient failures. Under load (rate limits, brief 5xx blips), a single
transient error fails the whole pipeline run that's already burned 5-10 of
tokens upstream. Claude calls already had retry in BaseAgent.run() — this
module brings Gemini and any other future LLM backend to parity.

Usage:
    from pipeline.llm_retry import call_with_retry

    def my_call():
        return gemini_client.call_gemini_json(...)

    result = call_with_retry(my_call, max_attempts=3)

Design:
- ONLY retries on transient-looking errors (rate limit / 5xx / timeout /
  connection). Auth / validation errors fail-fast — retrying won't help.
- Exponential backoff capped at `max_wait` so a flapping upstream doesn't
  blow up the total runtime.
- Synchronous (uses time.sleep). For async callers, wrap the call with
  asyncio.to_thread or use the async variant `acall_with_retry`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Substrings we treat as "transient — worth retrying". Case-insensitive
# match against str(exception). Lowercase keys so the lookup is cheap.
# These cover Gemini (google-genai), Anthropic SDK, requests/httpx, and
# the typical OpenAI-compat relay error shapes.
_RETRYABLE_SUBSTRINGS: tuple[str, ...] = (
    "429",
    "503",
    "504",
    "502",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "timeout",
    "timed out",
    "connection",
    "overloaded",
    "resource exhausted",
    "deadline exceeded",
    "internal error",
    "unavailable",
    # Vertex / Google specific
    "resource_exhausted",
    "service unavailable",
)


def _is_transient(exc: BaseException) -> bool:
    """Inspect exception text for transient-error fingerprints. Conservative:
    when in doubt, return False (don't retry). Better to fail-fast on a real
    bug than burn retry budget on something that won't recover."""
    msg = str(exc).lower()
    return any(s in msg for s in _RETRYABLE_SUBSTRINGS)


def call_with_retry(
    fn: Callable[..., T],
    *args,
    max_attempts: int = 3,
    initial_wait: float = 2.0,
    max_wait: float = 60.0,
    operation: str = "llm_call",
    **kwargs,
) -> T:
    """Run `fn(*args, **kwargs)` with bounded exponential backoff on
    transient errors.

    Total wall-clock ceiling = sum_{i=0..max_attempts-2} min(initial_wait * 2^i, max_wait)
    With defaults (3 attempts, 2s/60s): ~6s. With (5, 2s/60s): ~30s.

    - max_attempts: total tries including the first. Must be ≥1.
    - initial_wait: seconds after the first failure
    - max_wait: ceiling per individual sleep (防 backoff 爆炸)
    - operation: label for log lines, helps debugging which call was retrying

    Retries only on transient errors (see `_is_transient`). Other exceptions
    propagate immediately so callers see auth / validation issues without
    waiting for backoff.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt == max_attempts - 1:
                raise
            wait = min(initial_wait * (2 ** attempt), max_wait)
            logger.warning(
                "[%s] attempt %d/%d failed (%s: %s); retry in %.1fs",
                operation,
                attempt + 1,
                max_attempts,
                type(exc).__name__,
                str(exc)[:200],
                wait,
            )
            time.sleep(wait)
    # Defensive: loop above either returns or raises. If we got here something
    # is very wrong with the control flow — re-raise last_exc so the caller
    # gets a real error instead of None.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"[{operation}] call_with_retry exited unreachable branch")


async def acall_with_retry(
    fn: Callable[..., Awaitable[T]],
    *args,
    max_attempts: int = 3,
    initial_wait: float = 2.0,
    max_wait: float = 60.0,
    operation: str = "llm_call",
    **kwargs,
) -> T:
    """Async sibling of `call_with_retry`. Use when the wrapped function
    is itself an async coroutine. Uses asyncio.sleep so it doesn't block
    the event loop during backoff."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt == max_attempts - 1:
                raise
            wait = min(initial_wait * (2 ** attempt), max_wait)
            logger.warning(
                "[%s] attempt %d/%d failed (%s: %s); retry in %.1fs",
                operation,
                attempt + 1,
                max_attempts,
                type(exc).__name__,
                str(exc)[:200],
                wait,
            )
            await asyncio.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"[{operation}] acall_with_retry exited unreachable branch")

"""Exponential-backoff retry wrapper for Anthropic API calls (2s / 8s / 32s).

Distinguishes retryable transient errors (HTTP 5xx, timeouts, connection
errors) from unrecoverable ones (auth, quota, billing) and raises
:class:`UnrecoverableError` for the latter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine

import anthropic

__all__ = ["RetryExhaustedError", "UnrecoverableError", "with_retry"]

_log = logging.getLogger(__name__)

_RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0, 32.0)


class RetryExhaustedError(Exception):
    """All retry attempts failed; the last cause is chained."""


class UnrecoverableError(Exception):
    """Auth/quota/billing failure — do not retry; surface to Dev."""

    def __init__(self, message: str, status_code: int) -> None:
        """Initialise with an HTTP status code for context.

        Args:
            message (str): Human-readable description.
            status_code (int): HTTP status from the Anthropic API.
        """
        super().__init__(message)
        self.status_code = status_code


def _is_unrecoverable(exc: Exception) -> bool:
    return isinstance(
        exc,
        (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.RateLimitError),
    )


def _is_retryable(exc: Exception) -> bool:
    return isinstance(
        exc,
        (anthropic.InternalServerError, anthropic.APIConnectionError, anthropic.APITimeoutError),
    )


async def with_retry[T](
    factory: Callable[[], Coroutine[object, object, T]],
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> T:
    """Call ``factory()`` with exponential-backoff retries.

    ``factory`` is a zero-argument callable that returns a coroutine; it
    is called fresh on each attempt so a new SDK call is issued each time.

    Args:
        factory: Callable producing the coroutine to run.
        delays: Seconds to wait before each retry (one value per retry
            attempt, so ``len(delays)`` retries after the initial attempt).

    Returns:
        T: The value returned by a successful coroutine call.

    Raises:
        UnrecoverableError: Auth, permission, or quota failure.
        RetryExhaustedError: All attempts failed with retryable errors.
    """
    last_exc: Exception | None = None

    for attempt, delay in enumerate((-1.0, *delays)):
        if delay >= 0:
            _log.warning(
                "Anthropic API error (attempt %d/%d) — retrying in %.0fs",
                attempt,
                len(delays) + 1,
                delay,
            )
            await asyncio.sleep(delay)

        try:
            return await factory()
        except Exception as exc:
            if _is_unrecoverable(exc):
                status = (
                    exc.status_code
                    if hasattr(exc, "status_code")
                    else 0
                )
                raise UnrecoverableError(str(exc), status) from exc
            if _is_retryable(exc):
                last_exc = exc
                continue
            # Unknown error: surface immediately
            raise

    raise RetryExhaustedError(
        f"Anthropic API call failed after {len(delays) + 1} attempts"
    ) from last_exc


async def with_retry_iter[T](
    factory: Callable[[], AsyncIterator[T]],
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> AsyncIterator[T]:
    """Run an async-iterator factory with retries.

    Because async generators cannot be ``await``-ed, this helper wraps the
    iteration loop and restarts the factory on retryable errors.

    Args:
        factory: Callable producing an async iterator.
        delays: Seconds to wait before each retry.

    Yields:
        T: Items from the first successful iteration run.

    Raises:
        UnrecoverableError: Auth, permission, or quota failure.
        RetryExhaustedError: All attempts failed with retryable errors.
    """
    last_exc: Exception | None = None

    for attempt, delay in enumerate((-1.0, *delays)):
        if delay >= 0:
            _log.warning(
                "Anthropic stream error (attempt %d/%d) — retrying in %.0fs",
                attempt,
                len(delays) + 1,
                delay,
            )
            await asyncio.sleep(delay)

        try:
            async for item in factory():
                yield item
            return
        except Exception as exc:
            if _is_unrecoverable(exc):
                status = (
                    exc.status_code
                    if hasattr(exc, "status_code")
                    else 0
                )
                raise UnrecoverableError(str(exc), status) from exc
            if _is_retryable(exc):
                last_exc = exc
                continue
            raise

    raise RetryExhaustedError(
        f"Anthropic stream failed after {len(delays) + 1} attempts"
    ) from last_exc

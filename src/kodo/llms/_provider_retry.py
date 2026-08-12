"""Shared exponential-backoff retry core for cloud LLM plugins.

The Anthropic and OpenAI Python SDKs are both Stainless-generated with
matching exception shapes (``AuthenticationError``/``PermissionDeniedError``/
``BadRequestError``/``RateLimitError``/``InternalServerError``/
``APIConnectionError``/``APITimeoutError``, all deriving from an
``APIStatusError``-like base exposing ``.message``/``.status_code``), so the
retry/backoff/classification algorithm lives here once. Each vendor package
supplies its own SDK's exception classes via :class:`ProviderErrors` and gets
back the same behavior: unrecoverable errors (auth/quota/billing) raise
immediately, retryable errors (5xx/timeout/connection) back off and retry,
and 429s are translated into the gateway-owned :class:`RateLimited`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass

from kodo.llms._interface import RateLimited

__all__ = [
    "ProviderErrors",
    "RetryExhaustedError",
    "UnrecoverableError",
    "with_retry",
    "with_retry_iter",
]

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
            status_code (int): HTTP status from the provider API.
        """
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderErrors:
    """One vendor SDK's exception classes, all from that same SDK module.

    Attributes:
        rate_limit: The SDK's HTTP 429 exception class.
        unrecoverable: Auth/permission/bad-request classes — never retried.
        retryable: Transient 5xx/timeout/connection classes — retried with
            backoff.
        status_error: The SDK's base status-error class, exposing
            ``.message``/``.status_code`` (used to build
            :class:`UnrecoverableError` with the real status code).
        log_label: Human-readable vendor name for log lines, e.g. ``"Anthropic"``.
    """

    rate_limit: type[Exception]
    unrecoverable: tuple[type[Exception], ...]
    retryable: tuple[type[Exception], ...]
    status_error: type[Exception]
    log_label: str


def _as_rate_limited(exc: Exception) -> RateLimited:
    """Translate a provider 429 into the gateway-owned :class:`RateLimited`.

    Honors a ``Retry-After`` response header when present so the gateway can
    use the server-advised delay verbatim.
    """
    retry_after: float | None = None
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header:
        try:
            retry_after = float(header)
        except ValueError:
            retry_after = None
    return RateLimited(str(getattr(exc, "message", exc)), retry_after=retry_after)


def _as_unrecoverable(exc: Exception, errors: ProviderErrors) -> UnrecoverableError:
    if isinstance(exc, errors.status_error):
        message = getattr(exc, "message", str(exc))
        status_code = getattr(exc, "status_code", 0)
        return UnrecoverableError(message, status_code)
    return UnrecoverableError(str(exc), 0)


async def with_retry[T](
    factory: Callable[[], Coroutine[object, object, T]],
    errors: ProviderErrors,
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> T:
    """Call ``factory()`` with exponential-backoff retries.

    ``factory`` is a zero-argument callable that returns a coroutine; it
    is called fresh on each attempt so a new SDK call is issued each time.

    Args:
        factory: Callable producing the coroutine to run.
        errors: The calling vendor's SDK exception classes.
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
                "%s API error (attempt %d/%d) — retrying in %.0fs",
                errors.log_label,
                attempt,
                len(delays) + 1,
                delay,
            )
            await asyncio.sleep(delay)

        try:
            return await factory()
        except Exception as exc:
            if isinstance(exc, errors.rate_limit):
                raise _as_rate_limited(exc) from exc
            if isinstance(exc, errors.unrecoverable):
                raise _as_unrecoverable(exc, errors) from exc
            if isinstance(exc, errors.retryable):
                last_exc = exc
                continue
            # Unknown error: surface immediately
            raise

    raise RetryExhaustedError(
        f"{errors.log_label} API call failed after {len(delays) + 1} attempts"
    ) from last_exc


async def with_retry_iter[T](
    factory: Callable[[], AsyncIterator[T]],
    errors: ProviderErrors,
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> AsyncIterator[T]:
    """Run an async-iterator factory with retries.

    Because async generators cannot be ``await``-ed, this helper wraps the
    iteration loop and restarts the factory on retryable errors.

    Args:
        factory: Callable producing an async iterator.
        errors: The calling vendor's SDK exception classes.
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
                "%s stream error (attempt %d/%d) — retrying in %.0fs",
                errors.log_label,
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
            if isinstance(exc, errors.rate_limit):
                raise _as_rate_limited(exc) from exc
            if isinstance(exc, errors.unrecoverable):
                raise _as_unrecoverable(exc, errors) from exc
            if isinstance(exc, errors.retryable):
                last_exc = exc
                continue
            raise

    raise RetryExhaustedError(
        f"{errors.log_label} stream failed after {len(delays) + 1} attempts"
    ) from last_exc

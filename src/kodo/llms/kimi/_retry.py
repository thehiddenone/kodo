"""Exponential-backoff retry wrapper for Kimi API calls (2s / 8s / 32s).

Thin wiring over the shared :mod:`kodo.llms._provider_retry` core — see that
module for the actual retry/backoff/classification algorithm. Kimi is reached
through the ``openai`` Python SDK pointed at a custom ``base_url`` (Moonshot's
own OpenAI-compatible endpoint, https://platform.moonshot.ai/docs/guide/migrating-from-openai-to-kimi),
so it raises the same ``openai.*`` exception classes the OpenAI/Meta/Google/
Alibaba/DeepSeek plugins' own ``_retry.py`` handle — this module mirrors those
exactly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine

import openai

from kodo.llms._provider_retry import (
    ProviderErrors,
    RetryExhaustedError,
    UnrecoverableError,
)
from kodo.llms._provider_retry import with_retry as _shared_with_retry
from kodo.llms._provider_retry import with_retry_iter as _shared_with_retry_iter

__all__ = ["RetryExhaustedError", "UnrecoverableError", "with_retry", "with_retry_iter"]

_RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0, 32.0)

_ERRORS = ProviderErrors(
    rate_limit=openai.RateLimitError,
    unrecoverable=(
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.BadRequestError,
    ),
    retryable=(
        openai.InternalServerError,
        openai.APIConnectionError,
        openai.APITimeoutError,
    ),
    status_error=openai.APIStatusError,
    log_label="Kimi",
)


async def with_retry[T](
    factory: Callable[[], Coroutine[object, object, T]],
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> T:
    """Call ``factory()`` with exponential-backoff retries.

    See :func:`kodo.llms._provider_retry.with_retry`.
    """
    return await _shared_with_retry(factory, _ERRORS, delays)


async def with_retry_iter[T](
    factory: Callable[[], AsyncIterator[T]],
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> AsyncIterator[T]:
    """Run an async-iterator factory with retries.

    See :func:`kodo.llms._provider_retry.with_retry_iter`.
    """
    async for item in _shared_with_retry_iter(factory, _ERRORS, delays):
        yield item

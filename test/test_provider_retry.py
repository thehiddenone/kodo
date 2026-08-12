"""Behavior tests for kodo.llms._provider_retry -- the shared retry/backoff core.

Uses synthetic exception classes (not a real vendor SDK) to test the
algorithm in isolation; kodo.llms.anthropic._retry and kodo.llms.openai._retry
each get their own vendor-specific test file exercising the same behavior
through the real SDK exception classes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from kodo.llms import RateLimited
from kodo.llms._provider_retry import (
    ProviderErrors,
    RetryExhaustedError,
    UnrecoverableError,
    with_retry,
    with_retry_iter,
)

# ---------------------------------------------------------------------------
# Synthetic vendor exception hierarchy
# ---------------------------------------------------------------------------


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        response = MagicMock()
        response.headers = {"retry-after": retry_after} if retry_after else {}
        self.response = response


class _RateLimitError(_StatusError):
    pass


class _AuthError(_StatusError):
    pass


class _BadRequestError(_StatusError):
    pass


class _InternalError(_StatusError):
    pass


class _ConnectionError(Exception):
    pass


_ERRORS = ProviderErrors(
    rate_limit=_RateLimitError,
    unrecoverable=(_AuthError, _BadRequestError),
    retryable=(_InternalError, _ConnectionError),
    status_error=_StatusError,
    log_label="TestVendor",
)

# ---------------------------------------------------------------------------
# with_retry -- success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_returns_value_on_first_success() -> None:
    async def _factory() -> str:
        return "hello"

    result = await with_retry(_factory, _ERRORS, delays=())
    assert result == "hello"


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_retryable_error() -> None:
    calls = [0]

    async def _factory() -> str:
        if calls[0] == 0:
            calls[0] += 1
            raise _InternalError("server error", 500)
        return "ok"

    result = await with_retry(_factory, _ERRORS, delays=(0.0,))
    assert result == "ok"


# ---------------------------------------------------------------------------
# with_retry -- exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_retry_exhausted_after_all_attempts_fail() -> None:
    async def _factory() -> str:
        raise _InternalError("server error", 500)

    with pytest.raises(RetryExhaustedError):
        await with_retry(_factory, _ERRORS, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_exhausted_error_chains_last_cause() -> None:
    err = _ConnectionError("timeout")

    async def _factory() -> str:
        raise err

    try:
        await with_retry(_factory, _ERRORS, delays=(0.0,))
        pytest.fail("expected RetryExhaustedError")
    except RetryExhaustedError as exc:
        assert exc.__cause__ is err


# ---------------------------------------------------------------------------
# with_retry -- unrecoverable / rate-limited / unknown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_auth_error() -> None:
    async def _factory() -> str:
        raise _AuthError("bad api key", 401)

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, _ERRORS, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_unrecoverable_carries_status_code() -> None:
    async def _factory() -> str:
        raise _AuthError("bad api key", 401)

    with pytest.raises(UnrecoverableError) as exc_info:
        await with_retry(_factory, _ERRORS, delays=())
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_bad_request() -> None:
    async def _factory() -> str:
        raise _BadRequestError("credit balance too low", 400)

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, _ERRORS, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_rate_limited_on_429() -> None:
    async def _factory() -> str:
        raise _RateLimitError("rate limit", 429)

    with pytest.raises(RateLimited):
        await with_retry(_factory, _ERRORS, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_rate_limited_honors_retry_after_header() -> None:
    async def _factory() -> str:
        raise _RateLimitError("rate limit", 429, retry_after="30")

    with pytest.raises(RateLimited) as exc_info:
        await with_retry(_factory, _ERRORS, delays=(0.0, 0.0))
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_with_retry_reraises_unknown_error_immediately() -> None:
    async def _factory() -> str:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        await with_retry(_factory, _ERRORS, delays=(0.0, 0.0))


# ---------------------------------------------------------------------------
# with_retry_iter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_iter_yields_all_items_on_success() -> None:
    async def _factory() -> AsyncIterator[int]:
        for i in range(3):
            yield i

    items: list[int] = []
    async for item in with_retry_iter(_factory, _ERRORS, delays=()):
        items.append(item)

    assert items == [0, 1, 2]


@pytest.mark.asyncio
async def test_with_retry_iter_retries_on_server_error() -> None:
    call_count = [0]

    async def _factory() -> AsyncIterator[str]:
        call_count[0] += 1
        if call_count[0] == 1:
            raise _InternalError("server error", 500)
        yield "item"

    items: list[str] = []
    async for item in with_retry_iter(_factory, _ERRORS, delays=(0.0,)):
        items.append(item)

    assert items == ["item"]


@pytest.mark.asyncio
async def test_with_retry_iter_exhausted_raises_retry_exhausted() -> None:
    async def _factory() -> AsyncIterator[str]:
        raise _ConnectionError("timeout")
        yield  # pragma: no cover -- makes this a generator function

    with pytest.raises(RetryExhaustedError):
        async for _ in with_retry_iter(_factory, _ERRORS, delays=(0.0,)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_unrecoverable_raises_immediately() -> None:
    async def _factory() -> AsyncIterator[str]:
        raise _AuthError("bad api key", 401)
        yield  # pragma: no cover

    with pytest.raises(UnrecoverableError):
        async for _ in with_retry_iter(_factory, _ERRORS, delays=(0.0, 0.0)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_unknown_error_propagates_immediately() -> None:
    async def _factory() -> AsyncIterator[str]:
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        async for _ in with_retry_iter(_factory, _ERRORS, delays=(0.0, 0.0)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_empty_iterator_yields_nothing() -> None:
    async def _factory() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover

    items: list[str] = []
    async for item in with_retry_iter(_factory, _ERRORS, delays=()):
        items.append(item)

    assert items == []

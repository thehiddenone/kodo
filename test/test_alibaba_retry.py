"""Behavior tests for kodo.llms.alibaba._retry.

Alibaba's OpenAI-compatible endpoint is reached through the `openai` Python
SDK pointed at a custom base_url, so it raises the same `openai.*` exception
classes the OpenAI/Meta/Google plugins' own retry modules handle -- this
mirrors test_google_retry.py's cases exactly against
kodo.llms.alibaba._retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import openai
import pytest

import kodo.llms._provider_retry as provider_retry
from kodo.llms.alibaba._retry import (
    RetryExhaustedError,
    UnrecoverableError,
    with_retry,
    with_retry_iter,
)

# ---------------------------------------------------------------------------
# Helpers: build OpenAI-shaped error instances with minimal valid arguments
# ---------------------------------------------------------------------------


def _status_response(code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    r.request = MagicMock()
    return r


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        message="bad api key", response=_status_response(401), body=None
    )


def _permission_error() -> openai.PermissionDeniedError:
    return openai.PermissionDeniedError(
        message="forbidden", response=_status_response(403), body=None
    )


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError(message="rate limit", response=_status_response(429), body=None)


def _bad_request_error() -> openai.BadRequestError:
    return openai.BadRequestError(
        message="invalid request", response=_status_response(400), body=None
    )


def _internal_error() -> openai.InternalServerError:
    return openai.InternalServerError(
        message="server error", response=_status_response(500), body=None
    )


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(message="timeout", request=MagicMock())


# ---------------------------------------------------------------------------
# Cross-vendor identity: _worker.py's generic UnrecoverableError catch
# depends on this being the SAME class object as Anthropic's/OpenAI's/Meta's/
# Google's.
# ---------------------------------------------------------------------------


def test_unrecoverable_error_is_shared_across_vendors() -> None:
    assert UnrecoverableError is provider_retry.UnrecoverableError


def test_retry_exhausted_error_is_shared_across_vendors() -> None:
    assert RetryExhaustedError is provider_retry.RetryExhaustedError


# ---------------------------------------------------------------------------
# with_retry -- success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_returns_value_on_first_success() -> None:
    async def _factory() -> str:
        return "hello"

    result = await with_retry(_factory, delays=())
    assert result == "hello"


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_retryable_error() -> None:
    calls = [0]

    async def _factory() -> str:
        if calls[0] == 0:
            calls[0] += 1
            raise _internal_error()
        return "ok"

    result = await with_retry(_factory, delays=(0.0,))
    assert result == "ok"


# ---------------------------------------------------------------------------
# with_retry -- exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_retry_exhausted_after_all_attempts_fail() -> None:
    async def _factory() -> str:
        raise _internal_error()

    with pytest.raises(RetryExhaustedError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_exhausted_error_chains_last_cause() -> None:
    err = _connection_error()

    async def _factory() -> str:
        raise err

    try:
        await with_retry(_factory, delays=(0.0,))
        pytest.fail("expected RetryExhaustedError")
    except RetryExhaustedError as exc:
        assert exc.__cause__ is err


# ---------------------------------------------------------------------------
# with_retry -- unrecoverable errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_auth_error() -> None:
    async def _factory() -> str:
        raise _auth_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_permission_denied() -> None:
    async def _factory() -> str:
        raise _permission_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_rate_limited_on_429() -> None:
    from kodo.llms import RateLimited

    async def _factory() -> str:
        raise _rate_limit_error()

    with pytest.raises(RateLimited):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_bad_request() -> None:
    async def _factory() -> str:
        raise _bad_request_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_reraises_unknown_error_immediately() -> None:
    async def _factory() -> str:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_unrecoverable_carries_status_code() -> None:
    async def _factory() -> str:
        raise _auth_error()

    with pytest.raises(UnrecoverableError) as exc_info:
        await with_retry(_factory, delays=())
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# with_retry_iter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_iter_yields_all_items_on_success() -> None:
    async def _factory() -> AsyncIterator[int]:
        for i in range(3):
            yield i

    items: list[int] = []
    async for item in with_retry_iter(_factory, delays=()):
        items.append(item)

    assert items == [0, 1, 2]


@pytest.mark.asyncio
async def test_with_retry_iter_retries_on_server_error() -> None:
    call_count = [0]

    async def _factory() -> AsyncIterator[str]:
        call_count[0] += 1
        if call_count[0] == 1:
            raise _internal_error()
        yield "item"

    items: list[str] = []
    async for item in with_retry_iter(_factory, delays=(0.0,)):
        items.append(item)

    assert items == ["item"]


@pytest.mark.asyncio
async def test_with_retry_iter_exhausted_raises_retry_exhausted() -> None:
    async def _factory() -> AsyncIterator[str]:
        raise _connection_error()
        yield  # pragma: no cover -- makes this a generator function

    with pytest.raises(RetryExhaustedError):
        async for _ in with_retry_iter(_factory, delays=(0.0,)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_unrecoverable_raises_immediately() -> None:
    async def _factory() -> AsyncIterator[str]:
        raise _auth_error()
        yield  # pragma: no cover

    with pytest.raises(UnrecoverableError):
        async for _ in with_retry_iter(_factory, delays=(0.0, 0.0)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_unknown_error_propagates_immediately() -> None:
    async def _factory() -> AsyncIterator[str]:
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        async for _ in with_retry_iter(_factory, delays=(0.0, 0.0)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_empty_iterator_yields_nothing() -> None:
    async def _factory() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover

    items: list[str] = []
    async for item in with_retry_iter(_factory, delays=()):
        items.append(item)

    assert items == []

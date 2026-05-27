"""Behavior tests for kodo.llms.anthropic._retry.

Tests verify retry behavior by observing outcomes (values returned, exceptions
raised) without asserting on call counts or internal implementation details.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import anthropic
import pytest

from kodo.llms.anthropic._retry import (
    RetryExhaustedError,
    UnrecoverableError,
    with_retry,
    with_retry_iter,
)

# ---------------------------------------------------------------------------
# Helpers: build Anthropic error instances with minimal valid arguments
# ---------------------------------------------------------------------------


def _status_response(code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    r.request = MagicMock()
    return r


def _auth_error() -> anthropic.AuthenticationError:
    return anthropic.AuthenticationError(
        message="bad api key", response=_status_response(401), body=None
    )


def _permission_error() -> anthropic.PermissionDeniedError:
    return anthropic.PermissionDeniedError(
        message="forbidden", response=_status_response(403), body=None
    )


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError(message="rate limit", response=_status_response(429), body=None)


def _bad_request_error() -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        message="credit balance too low", response=_status_response(400), body=None
    )


def _internal_error() -> anthropic.InternalServerError:
    return anthropic.InternalServerError(
        message="server error", response=_status_response(500), body=None
    )


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(message="timeout", request=MagicMock())


# ---------------------------------------------------------------------------
# with_retry — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_returns_value_on_first_success() -> None:
    """
    Given a factory that always succeeds,
    when with_retry is called,
    then the returned value equals what the factory produced.
    """

    async def _factory() -> str:
        return "hello"

    result = await with_retry(_factory, delays=())
    assert result == "hello"


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_retryable_error() -> None:
    """
    Given a factory that raises InternalServerError once then succeeds,
    when with_retry is called with at least one retry,
    then the final value is returned.
    """
    calls = [0]

    async def _factory() -> str:
        if calls[0] == 0:
            calls[0] += 1
            raise _internal_error()
        return "ok"

    result = await with_retry(_factory, delays=(0.0,))
    assert result == "ok"


# ---------------------------------------------------------------------------
# with_retry — exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_retry_exhausted_after_all_attempts_fail() -> None:
    """
    Given a factory that always raises a retryable error,
    when with_retry exhausts all delays,
    then RetryExhaustedError is raised.
    """

    async def _factory() -> str:
        raise _internal_error()

    with pytest.raises(RetryExhaustedError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_exhausted_error_chains_last_cause() -> None:
    """
    Given repeated retryable failures,
    when RetryExhaustedError is raised,
    then its __cause__ is the last underlying exception.
    """
    err = _connection_error()

    async def _factory() -> str:
        raise err

    try:
        await with_retry(_factory, delays=(0.0,))
    except RetryExhaustedError as exc:
        assert exc.__cause__ is err


# ---------------------------------------------------------------------------
# with_retry — unrecoverable errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_auth_error() -> None:
    """
    Given a factory that raises AuthenticationError,
    when with_retry is called,
    then UnrecoverableError is raised immediately.
    """

    async def _factory() -> str:
        raise _auth_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_permission_denied() -> None:
    """
    Given a factory that raises PermissionDeniedError,
    when with_retry is called,
    then UnrecoverableError is raised without retrying.
    """

    async def _factory() -> str:
        raise _permission_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_rate_limit() -> None:
    """
    Given a factory that raises RateLimitError,
    when with_retry is called,
    then UnrecoverableError is raised immediately.
    """

    async def _factory() -> str:
        raise _rate_limit_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_on_bad_request() -> None:
    """
    Given a factory that raises BadRequestError (e.g. billing issue),
    when with_retry is called,
    then UnrecoverableError is raised.
    """

    async def _factory() -> str:
        raise _bad_request_error()

    with pytest.raises(UnrecoverableError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_reraises_unknown_error_immediately() -> None:
    """
    Given a factory that raises an arbitrary non-Anthropic exception,
    when with_retry is called,
    then the exception propagates immediately without retry.
    """

    async def _factory() -> str:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        await with_retry(_factory, delays=(0.0, 0.0))


@pytest.mark.asyncio
async def test_with_retry_unrecoverable_carries_status_code() -> None:
    """
    Given a factory that raises an Anthropic status error,
    when UnrecoverableError is raised,
    then its status_code matches the HTTP response code.
    """

    async def _factory() -> str:
        raise _auth_error()

    with pytest.raises(UnrecoverableError) as exc_info:
        await with_retry(_factory, delays=())
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# UnrecoverableError direct construction
# ---------------------------------------------------------------------------


def test_unrecoverable_error_stores_status_code() -> None:
    """
    Given an UnrecoverableError with a status code,
    when the object is created,
    then status_code attribute holds the given value.
    """
    exc = UnrecoverableError("billing failure", 400)
    assert exc.status_code == 400


def test_unrecoverable_error_message_is_accessible() -> None:
    """
    Given an UnrecoverableError,
    when str() is called on it,
    then the message string is present in the output.
    """
    exc = UnrecoverableError("auth error", 401)
    assert "auth error" in str(exc)


# ---------------------------------------------------------------------------
# with_retry_iter — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_iter_yields_all_items_on_success() -> None:
    """
    Given a factory that produces a finite async iterator,
    when with_retry_iter is called,
    then all items are yielded in order.
    """

    async def _factory() -> AsyncIterator[int]:
        for i in range(3):
            yield i

    items: list[int] = []
    async for item in with_retry_iter(_factory, delays=()):
        items.append(item)

    assert items == [0, 1, 2]


@pytest.mark.asyncio
async def test_with_retry_iter_retries_on_server_error() -> None:
    """
    Given a factory that raises InternalServerError on first call then yields items,
    when with_retry_iter is called with one retry,
    then items from the successful iteration are yielded.
    """
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
    """
    Given a factory that always raises a retryable error,
    when with_retry_iter runs out of attempts,
    then RetryExhaustedError is raised.
    """

    async def _factory() -> AsyncIterator[str]:
        raise _connection_error()
        yield  # make it a generator

    with pytest.raises(RetryExhaustedError):
        async for _ in with_retry_iter(_factory, delays=(0.0,)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_unrecoverable_raises_immediately() -> None:
    """
    Given a factory that raises AuthenticationError during iteration,
    when with_retry_iter is called,
    then UnrecoverableError is raised without further attempts.
    """

    async def _factory() -> AsyncIterator[str]:
        raise _auth_error()
        yield

    with pytest.raises(UnrecoverableError):
        async for _ in with_retry_iter(_factory, delays=(0.0, 0.0)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_unknown_error_propagates_immediately() -> None:
    """
    Given a factory that raises a non-Anthropic exception during iteration,
    when with_retry_iter is called,
    then the exception propagates without retry.
    """

    async def _factory() -> AsyncIterator[str]:
        raise RuntimeError("boom")
        yield

    with pytest.raises(RuntimeError):
        async for _ in with_retry_iter(_factory, delays=(0.0, 0.0)):
            pass


@pytest.mark.asyncio
async def test_with_retry_iter_empty_iterator_yields_nothing() -> None:
    """
    Given a factory that produces an empty async iterator,
    when with_retry_iter is called,
    then no items are yielded and no error is raised.
    """

    async def _factory() -> AsyncIterator[str]:
        return
        yield

    items: list[str] = []
    async for item in with_retry_iter(_factory, delays=()):
        items.append(item)

    assert items == []

"""Tests for ``kodo.llms.bedrock._retry`` -- botocore error classification.

Bedrock is the one vendor here whose SDK does not hand out per-failure
exception classes, so ``translate_botocore_error`` is the substitute the
shared retry core classifies against. These tests pin the three tables and,
more importantly, the *unknown code* fallback -- a code kodo has never seen is
routed by HTTP status, and the direction of that fallback (5xx retried, 4xx
not) is a deliberate decision, not an accident.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoRegionError

from kodo.llms._interface import RateLimited
from kodo.llms._provider_retry import UnrecoverableError
from kodo.llms.bedrock._retry import (
    BedrockRateLimitError,
    BedrockRetryableError,
    BedrockUnrecoverableError,
    translate_botocore_error,
    with_retry,
)


def _client_error(code: str, status: int = 0, headers: dict[str, str] | None = None) -> ClientError:
    metadata: dict[str, object] = {}
    if status:
        metadata["HTTPStatusCode"] = status
    if headers is not None:
        metadata["HTTPHeaders"] = headers
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} happened"}, "ResponseMetadata": metadata},
        "ConverseStream",
    )


# ---------------------------------------------------------------------------
# translate_botocore_error -- known codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["ThrottlingException", "TooManyRequestsException", "ModelNotReadyException"],
)
def test_throttling_codes_become_rate_limit(code: str) -> None:
    assert isinstance(translate_botocore_error(_client_error(code, 429)), BedrockRateLimitError)


@pytest.mark.parametrize(
    "code",
    ["AccessDeniedException", "UnrecognizedClientException", "ValidationException"],
)
def test_auth_and_validation_codes_are_unrecoverable(code: str) -> None:
    assert isinstance(translate_botocore_error(_client_error(code, 400)), BedrockUnrecoverableError)


@pytest.mark.parametrize(
    "code",
    ["InternalServerException", "ServiceUnavailableException", "ModelStreamErrorException"],
)
def test_transient_service_codes_are_retryable(code: str) -> None:
    assert isinstance(translate_botocore_error(_client_error(code, 500)), BedrockRetryableError)


def test_translation_preserves_message_and_status() -> None:
    translated = translate_botocore_error(_client_error("AccessDeniedException", 403))
    assert isinstance(translated, BedrockUnrecoverableError)
    assert translated.status_code == 403
    assert "AccessDeniedException" in translated.message


def test_retry_after_header_is_carried_through() -> None:
    """The gateway uses this as a backoff *floor* (kodo/llms/_gateway.py)."""
    translated = translate_botocore_error(
        _client_error("ThrottlingException", 429, {"retry-after": "30"})
    )
    assert isinstance(translated, BedrockRateLimitError)
    assert translated.response.headers["retry-after"] == "30"


# ---------------------------------------------------------------------------
# translate_botocore_error -- unknown codes fall back to HTTP status
# ---------------------------------------------------------------------------


def test_unknown_5xx_code_is_retryable() -> None:
    assert isinstance(
        translate_botocore_error(_client_error("SomeFutureFailure", 502)), BedrockRetryableError
    )


def test_unknown_4xx_code_is_unrecoverable() -> None:
    """Retrying an identical malformed request four times only delays the error."""
    assert isinstance(
        translate_botocore_error(_client_error("SomeFutureRejection", 404)),
        BedrockUnrecoverableError,
    )


def test_unknown_code_with_429_status_is_rate_limited() -> None:
    assert isinstance(
        translate_botocore_error(_client_error("SomeFutureThrottle", 429)), BedrockRateLimitError
    )


def test_unknown_code_with_no_status_is_unrecoverable() -> None:
    assert isinstance(translate_botocore_error(_client_error("Mystery")), BedrockUnrecoverableError)


# ---------------------------------------------------------------------------
# translate_botocore_error -- non-ClientError botocore failures
# ---------------------------------------------------------------------------


def test_connection_error_is_retryable() -> None:
    exc = EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
    assert isinstance(translate_botocore_error(exc), BedrockRetryableError)


def test_configuration_botocore_error_is_unrecoverable() -> None:
    assert isinstance(translate_botocore_error(NoRegionError()), BedrockUnrecoverableError)


def test_non_botocore_exception_passes_through_unchanged() -> None:
    """A programming error must not be laundered into a retryable one."""
    exc = ValueError("bug")
    assert translate_botocore_error(exc) is exc


# ---------------------------------------------------------------------------
# with_retry -- the shared core classifies this module's hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_raises_unrecoverable_immediately() -> None:
    attempts = 0

    async def factory() -> str:
        nonlocal attempts
        attempts += 1
        raise BedrockUnrecoverableError("denied", 403)

    with pytest.raises(UnrecoverableError):
        await with_retry(factory, delays=(0.0, 0.0))
    assert attempts == 1


@pytest.mark.asyncio
async def test_with_retry_translates_429_into_rate_limited() -> None:
    async def factory() -> str:
        raise BedrockRateLimitError("slow down", 429)

    with pytest.raises(RateLimited):
        await with_retry(factory, delays=(0.0,))


@pytest.mark.asyncio
async def test_with_retry_retries_transient_then_succeeds() -> None:
    attempts = 0

    async def factory() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise BedrockRetryableError("try again", 503)
        return "ok"

    assert await with_retry(factory, delays=(0.0, 0.0, 0.0)) == "ok"
    assert attempts == 3

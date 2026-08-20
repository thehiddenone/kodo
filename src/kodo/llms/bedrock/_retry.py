"""Exponential-backoff retry wrapper for AWS Bedrock calls (2s / 8s / 32s).

Thin wiring over the shared :mod:`kodo.llms._provider_retry` core — see that
module for the actual retry/backoff/classification algorithm. Bedrock is the
one vendor here **not** reached through a Stainless-generated SDK, so unlike
every other ``_retry.py`` in this package it cannot simply hand
:class:`~kodo.llms._provider_retry.ProviderErrors` a set of SDK exception
classes: botocore raises one ``ClientError`` for every service error and puts
the discriminator in ``response["Error"]["Code"]`` (a *string*), and its
per-service subclasses (``client.exceptions.ThrottlingException`` &c.) are
generated at runtime off the service model, so they aren't importable at
module scope at all.

This module therefore does the classification itself, in
:func:`translate_botocore_error`, and re-raises into a small statically
importable hierarchy (:class:`BedrockRateLimitError`,
:class:`BedrockUnrecoverableError`, :class:`BedrockRetryableError`) that the
shared core can then classify by class exactly like every other vendor's. The
plugin calls that translator around every boto3 call, so nothing outside this
package ever sees a raw ``ClientError``.

Codes not in either table fall back to the HTTP status: 5xx is retried, 4xx is
not. That is deliberately the safe direction — an unrecognised 4xx is a
configuration problem (a bad model id, a missing model-access grant) that four
identical retries would only delay.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    HTTPClientError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from kodo.llms._provider_retry import (
    ProviderErrors,
    RetryExhaustedError,
    UnrecoverableError,
)
from kodo.llms._provider_retry import with_retry as _shared_with_retry
from kodo.llms._provider_retry import with_retry_iter as _shared_with_retry_iter

__all__ = [
    "BedrockRateLimitError",
    "BedrockRetryableError",
    "BedrockStatusError",
    "BedrockUnrecoverableError",
    "RetryExhaustedError",
    "UnrecoverableError",
    "translate_botocore_error",
    "with_retry",
    "with_retry_iter",
]

_RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0, 32.0)

# Bedrock throttling / capacity pressure. ModelNotReadyException is a 429 in
# Bedrock's own error table (the model is warming up), so it belongs here
# rather than with the transient 5xx group.
# https://docs.aws.amazon.com/bedrock/latest/userguide/troubleshooting-api-error-codes.html
_RATE_LIMIT_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "ModelNotReadyException",
        "ProvisionedThroughputExceededException",
        "ServiceQuotaExceededException",
        "RequestThrottled",
        "RequestThrottledException",
    }
)

# Auth, signing, and request-shape failures — never worth retrying.
# ValidationException is here rather than in the retryable set on purpose: on
# Bedrock it is what an unsupported `additionalModelRequestFields` payload or
# a model id that needs an inference profile returns, and retrying an
# identical malformed request four times just delays the error.
_UNRECOVERABLE_CODES = frozenset(
    {
        "AccessDeniedException",
        "AccessDenied",
        "AuthFailure",
        "ExpiredToken",
        "ExpiredTokenException",
        "IncompleteSignature",
        "InvalidClientTokenId",
        "InvalidSignatureException",
        "MissingAuthenticationToken",
        "ResourceNotFoundException",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
        "ValidationException",
    }
)

# Transient server-side failures. ModelErrorException/ModelStreamErrorException
# are 424s raised when an upstream model fails mid-generation; a fresh attempt
# routinely succeeds.
_RETRYABLE_CODES = frozenset(
    {
        "InternalFailure",
        "InternalServerException",
        "ModelErrorException",
        "ModelStreamErrorException",
        "ModelTimeoutException",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
    }
)


@dataclass(frozen=True)
class _ErrorResponse:
    """The ``.response.headers`` shape :func:`_provider_retry._as_rate_limited` reads.

    botocore's ``ClientError.response`` is a plain dict whose headers live at
    ``["ResponseMetadata"]["HTTPHeaders"]``; the shared core expects an object
    exposing ``.headers``. This adapter is that one attribute and nothing
    more, so a ``Retry-After`` Bedrock does send is still honored as the
    gateway's backoff *floor* (kodo/llms/_gateway.py's ``bump_backoff``).
    """

    headers: dict[str, str] = field(default_factory=dict)


class BedrockStatusError(Exception):
    """Base for the translated errors — carries ``.message``/``.status_code``.

    Matches the attribute surface :class:`~kodo.llms._provider_retry.ProviderErrors`'
    ``status_error`` slot expects of a vendor SDK's base status exception.
    """

    def __init__(
        self,
        message: str,
        status_code: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialise with the provider's own message and HTTP status.

        Args:
            message (str): Human-readable description from the service.
            status_code (int): HTTP status the service returned.
            headers (dict[str, str] | None): Response headers, used only to
                surface ``Retry-After`` on a 429.
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = _ErrorResponse(headers or {})


class BedrockRateLimitError(BedrockStatusError):
    """Bedrock throttled the request (HTTP 429)."""


class BedrockUnrecoverableError(BedrockStatusError):
    """Auth/permission/validation failure — do not retry."""


class BedrockRetryableError(BedrockStatusError):
    """Transient service or connection failure — retry with backoff."""


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error") if isinstance(exc.response, dict) else None
    return str(error.get("Code", "")) if isinstance(error, dict) else ""


def _error_message(exc: ClientError) -> str:
    error = exc.response.get("Error") if isinstance(exc.response, dict) else None
    message = str(error.get("Message", "")) if isinstance(error, dict) else ""
    return message or str(exc)


def _response_metadata(exc: ClientError) -> dict[str, object]:
    meta = exc.response.get("ResponseMetadata") if isinstance(exc.response, dict) else None
    return meta if isinstance(meta, dict) else {}


def _status_code(exc: ClientError) -> int:
    raw = _response_metadata(exc).get("HTTPStatusCode")
    return raw if isinstance(raw, int) else 0


def _headers(exc: ClientError) -> dict[str, str]:
    raw = _response_metadata(exc).get("HTTPHeaders")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def translate_botocore_error(exc: BaseException) -> BaseException:
    """Map a botocore exception onto this module's classifiable hierarchy.

    Args:
        exc (BaseException): Whatever a boto3 call raised.

    Returns:
        BaseException: A :class:`BedrockStatusError` subclass for anything
        recognisable, or *exc* itself when it isn't a botocore error at all
        (a programming error must not be laundered into a retryable one).
    """
    if isinstance(exc, ClientError):
        code = _error_code(exc)
        message = _error_message(exc)
        status = _status_code(exc)
        headers = _headers(exc)
        if code in _RATE_LIMIT_CODES:
            return BedrockRateLimitError(message, status or 429, headers)
        if code in _UNRECOVERABLE_CODES:
            return BedrockUnrecoverableError(message, status or 400, headers)
        if code in _RETRYABLE_CODES:
            return BedrockRetryableError(message, status or 500, headers)
        # Unknown code: trust the HTTP status. 5xx is transient, everything
        # else (a 4xx we don't have a name for) is a configuration problem.
        if status >= 500:
            return BedrockRetryableError(message, status, headers)
        if status == 429:
            return BedrockRateLimitError(message, status, headers)
        return BedrockUnrecoverableError(message, status or 400, headers)

    # Socket/DNS/TLS level failures never reach the service, so they carry no
    # service error code — but they are exactly the transient class the
    # backoff exists for.
    if isinstance(exc, BotoConnectionError | ConnectionClosedError | HTTPClientError):
        return BedrockRetryableError(str(exc), 0)

    # Any other BotoCoreError (bad region, unresolvable endpoint, missing
    # credentials) is a configuration fault: surfacing it immediately is more
    # useful than 42 seconds of identical retries.
    if isinstance(exc, BotoCoreError):
        return BedrockUnrecoverableError(str(exc), 0)

    return exc


_ERRORS = ProviderErrors(
    rate_limit=BedrockRateLimitError,
    unrecoverable=(BedrockUnrecoverableError,),
    retryable=(BedrockRetryableError,),
    status_error=BedrockStatusError,
    log_label="Bedrock",
)


async def with_retry[T](
    factory: Callable[[], Coroutine[object, object, T]],
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> T:
    """Call ``factory()`` with exponential-backoff retries.

    See :func:`kodo.llms._provider_retry.with_retry`. The coroutine must
    already raise this module's translated errors — see
    :func:`translate_botocore_error`.
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

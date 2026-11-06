"""Tests for ``kodo.llms.bedrock``'s token check and bearer client.

A Bedrock credential is one bearer token (doc/LLM_REGISTRY.md §3b). The client
tests run real botocore request building against a stubbed transport, because
what matters is what actually goes on the wire: the bearer header, the regional
endpoint, and nothing taken from the process environment.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.awsrequest import AWSRequest, AWSResponse
from botocore.config import Config

from kodo.llms.bedrock import InvalidCredentialsError, bearer_client, require_bedrock_token


def test_a_token_is_returned_stripped() -> None:
    assert require_bedrock_token("  bedrock-api-key-abc\n") == "bedrock-api-key-abc"


@pytest.mark.parametrize("value", ["", "   \n"])
def test_an_empty_token_is_rejected(value: str) -> None:
    with pytest.raises(InvalidCredentialsError):
        require_bedrock_token(value)


def test_an_old_access_key_pair_is_rejected_with_a_way_forward() -> None:
    with pytest.raises(InvalidCredentialsError, match="AWS_BEARER_TOKEN_BEDROCK"):
        require_bedrock_token('{"access_key_id": "AKIA", "secret_access_key": "s"}')


class _Body(io.BytesIO):
    def stream(self, **_kwargs: Any) -> Iterator[bytes]:
        yield self.read()


def _record_request(client: Any, event: str) -> list[AWSRequest]:
    seen: list[AWSRequest] = []

    def _send(request: AWSRequest, **_kwargs: Any) -> AWSResponse:
        seen.append(request)
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, _Body(b"{}"))

    client.meta.events.register(event, _send)
    return seen


def _authorization(request: AWSRequest) -> bytes:
    value = request.headers["Authorization"]
    return value if isinstance(value, bytes) else value.encode()


def test_a_runtime_call_sends_the_token_to_the_regional_endpoint() -> None:
    client = bearer_client("bedrock-runtime", "bedrock-api-key-a", "eu-west-1", Config())
    seen = _record_request(client, "before-send.bedrock-runtime.Converse")
    client.converse(modelId="m", messages=[])
    assert _authorization(seen[0]) == b"Bearer bedrock-api-key-a"
    assert seen[0].url.startswith("https://bedrock-runtime.eu-west-1.amazonaws.com/")


@pytest.mark.parametrize(
    ("event", "operation"),
    [
        ("before-send.bedrock.ListFoundationModels", "list_foundation_models"),
        ("before-send.bedrock.ListInferenceProfiles", "list_inference_profiles"),
    ],
)
def test_catalog_calls_send_the_token(event: str, operation: str) -> None:
    client = bearer_client("bedrock", "bedrock-api-key-a", "us-east-1", Config())
    seen = _record_request(client, event)
    getattr(client, operation)()
    assert _authorization(seen[0]) == b"Bearer bedrock-api-key-a"


def test_two_clients_in_one_process_keep_their_own_tokens() -> None:
    first = bearer_client("bedrock-runtime", "bedrock-api-key-a", "us-east-1", Config())
    second = bearer_client("bedrock-runtime", "bedrock-api-key-b", "us-east-1", Config())
    first_seen = _record_request(first, "before-send.bedrock-runtime.Converse")
    second_seen = _record_request(second, "before-send.bedrock-runtime.Converse")
    first.converse(modelId="m", messages=[])
    second.converse(modelId="m", messages=[])
    assert _authorization(first_seen[0]) == b"Bearer bedrock-api-key-a"
    assert _authorization(second_seen[0]) == b"Bearer bedrock-api-key-b"


def test_a_stale_profile_in_the_server_environment_does_not_break_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "no-such-profile")
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "no-such-profile")
    client = bearer_client("bedrock-runtime", "bedrock-api-key-a", "us-east-1", Config())
    seen = _record_request(client, "before-send.bedrock-runtime.Converse")
    client.converse(modelId="m", messages=[])
    assert _authorization(seen[0]) == b"Bearer bedrock-api-key-a"

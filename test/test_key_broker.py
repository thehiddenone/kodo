"""Behavior tests for kodo.server.KeyBroker.

Exercises the request/response round-trip and, notably, that only a genuine
cancellation (the session's worker task being torn down) ends the wait —
an ordinary SessionChannel-level disconnect no longer does, since the
pending future now lives on the session-scoped channel, not the socket-
scoped Connection (see kodo.transport._connection, doc/SECURITY.md §7).
"""

from __future__ import annotations

import asyncio

import pytest

from kodo.common import ApiKey
from kodo.server._key_broker import KeyBroker
from kodo.transport import SREQ_API_KEY_REQUEST, SessionChannel


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_get_key_sends_request_and_returns_key_on_success() -> None:
    from kodo.transport import Connection

    channel = SessionChannel()
    await channel.attach(Connection(_FakeWS()))  # type: ignore[arg-type]
    broker = KeyBroker(channel)

    async def _run() -> ApiKey:
        task = asyncio.create_task(broker.get_key("anthropic"))
        await asyncio.sleep(0)
        # Find the pending request's id via the channel's internal replay set
        # (public surface: resolve by id we captured from the sent envelope).
        env_json = channel.connection.ws.sent[0]  # type: ignore[union-attr]
        import json

        req_id = json.loads(env_json)["id"]
        channel.resolve_response(req_id, {"api_key": "sk-test-123"})
        return await task

    result = await _run()
    assert result.api_key == "sk-test-123"
    assert result.error is None


@pytest.mark.asyncio
async def test_get_key_request_payload_carries_vendor() -> None:
    from kodo.transport import Connection

    channel = SessionChannel()
    await channel.attach(Connection(_FakeWS()))  # type: ignore[arg-type]
    broker = KeyBroker(channel)

    task = asyncio.create_task(broker.get_key("openai"))
    await asyncio.sleep(0)
    import json

    env = json.loads(channel.connection.ws.sent[0])  # type: ignore[union-attr]
    assert env["payload"]["type"] == SREQ_API_KEY_REQUEST
    assert env["payload"]["vendor"] == "openai"

    channel.resolve_response(env["id"], {"api_key": "k"})
    await task


@pytest.mark.asyncio
async def test_get_key_returns_error_when_client_rejects() -> None:
    from kodo.transport import Connection

    channel = SessionChannel()
    await channel.attach(Connection(_FakeWS()))  # type: ignore[arg-type]
    broker = KeyBroker(channel)

    task = asyncio.create_task(broker.get_key("anthropic"))
    await asyncio.sleep(0)
    import json

    req_id = json.loads(channel.connection.ws.sent[0])["id"]  # type: ignore[union-attr]
    channel.resolve_response(req_id, {"error": "user_cancelled"})

    result = await task
    assert result.error == "user_cancelled"
    assert result.api_key == ""


@pytest.mark.asyncio
async def test_get_key_survives_disconnect_and_resolves_after_reconnect() -> None:
    """The scenario this whole fix is about: the window reloads mid-request
    (detach, no cancellation) and the eventual answer — over a brand-new
    connection — still completes the original get_key() call."""
    from kodo.transport import Connection

    channel = SessionChannel()
    conn1 = Connection(_FakeWS())  # type: ignore[arg-type]
    await channel.attach(conn1)
    broker = KeyBroker(channel)

    task = asyncio.create_task(broker.get_key("anthropic"))
    await asyncio.sleep(0)
    import json

    req_id = json.loads(conn1.ws.sent[0])["id"]  # type: ignore[attr-defined]

    channel.detach(conn1)  # disconnect — nothing cancels the wait
    await asyncio.sleep(0)
    assert not task.done()

    conn2 = Connection(_FakeWS())  # type: ignore[arg-type]
    await channel.attach(conn2)
    channel.resolve_response(req_id, {"api_key": "sk-after-reconnect"})

    result = await task
    assert result.api_key == "sk-after-reconnect"


@pytest.mark.asyncio
async def test_get_key_cancellation_returns_connection_lost_error() -> None:
    """Genuine teardown (the session's worker task cancelled) still surfaces
    as a clean key-request failure, not a propagated CancelledError."""
    from kodo.transport import Connection

    channel = SessionChannel()
    await channel.attach(Connection(_FakeWS()))  # type: ignore[arg-type]
    broker = KeyBroker(channel)

    task = asyncio.create_task(broker.get_key("anthropic"))
    await asyncio.sleep(0)
    task.cancel()

    result = await task
    assert result.error == "connection_lost"
    assert result.api_key == ""

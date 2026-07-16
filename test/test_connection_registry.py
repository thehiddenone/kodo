"""Behavior tests for kodo.server.ConnectionRegistry's frame dispatch.

Focused on `kind="response"` routing: a client's answer to a server-initiated
request (approval/question/permission/API key) must resolve the future on
the *session's* SessionChannel — found via the connection it arrived on —
not (as before this fix) on the Connection object itself, which no longer
owns any pending-future state at all (see kodo.transport._connection and
doc/SECURITY.md §7 / WS_PROTOCOL.md §8).

Uses a duck-typed fake manager/session rather than a real SessionManager —
ConnectionRegistry only ever calls `manager.session_for_connection(conn.id)`
and reads `session.channel`, so a full engine/gateway stack would be
incidental weight here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kodo.common import Envelope
from kodo.server import ConnectionRegistry
from kodo.transport import Connection


class _FakeWS:
    closed = False

    async def send_str(self, _data: str) -> None:
        return None


def _conn() -> Connection:
    return Connection(_FakeWS())  # type: ignore[arg-type]


class _FakeChannel:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, dict[str, object]]] = []

    def resolve_response(self, correlation_id: str, payload: dict[str, object]) -> None:
        self.resolved.append((correlation_id, payload))


class _FakeManager:
    def __init__(self, bound: dict[str, object] | None = None) -> None:
        self._bound = bound or {}

    def session_for_connection(self, conn_id: str) -> object | None:
        return self._bound.get(conn_id)


def _response_env(correlation_id: str) -> Envelope:
    return Envelope(
        kind="response",
        id="resp-1",
        correlation_id=correlation_id,
        payload={"action": "allow"},
    )


@pytest.mark.asyncio
async def test_response_resolves_via_the_bound_sessions_channel() -> None:
    channel = _FakeChannel()
    conn = _conn()
    manager = _FakeManager({conn.id: SimpleNamespace(channel=channel)})
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    await registry._ConnectionRegistry__dispatch(conn, _response_env("req-1").to_json())

    assert channel.resolved == [("req-1", {"action": "allow"})]


@pytest.mark.asyncio
async def test_response_on_a_connection_bound_to_no_session_does_not_raise() -> None:
    """A response arriving after the connection's session binding is gone
    (e.g. a very late/duplicate answer) is dropped, not a crash."""
    manager = _FakeManager({})
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    await registry._ConnectionRegistry__dispatch(_conn(), _response_env("req-1").to_json())


@pytest.mark.asyncio
async def test_response_with_empty_correlation_id_does_not_resolve_anything() -> None:
    channel = _FakeChannel()
    conn = _conn()
    manager = _FakeManager({conn.id: SimpleNamespace(channel=channel)})
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    env = Envelope(kind="response", id="resp-1", correlation_id="", payload={})
    await registry._ConnectionRegistry__dispatch(conn, env.to_json())

    assert channel.resolved == []


@pytest.mark.asyncio
async def test_two_connections_each_resolve_only_their_own_session() -> None:
    channel_a = _FakeChannel()
    channel_b = _FakeChannel()
    conn_a, conn_b = _conn(), _conn()
    manager = _FakeManager(
        {
            conn_a.id: SimpleNamespace(channel=channel_a),
            conn_b.id: SimpleNamespace(channel=channel_b),
        }
    )
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    await registry._ConnectionRegistry__dispatch(conn_a, _response_env("req-a").to_json())
    await registry._ConnectionRegistry__dispatch(conn_b, _response_env("req-b").to_json())

    assert channel_a.resolved == [("req-a", {"action": "allow"})]
    assert channel_b.resolved == [("req-b", {"action": "allow"})]

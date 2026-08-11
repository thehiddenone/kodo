"""Behavior tests for kodo.server.ConnectionRegistry's frame dispatch.

Focused on `kind="response"` routing: a client's answer to a server-initiated
request (approval/question/permission/API key) must resolve the future on
the *session's* SessionChannel — found via the connection it arrived on —
not (as before this fix) on the Connection object itself, which no longer
owns any pending-future state at all (see kodo.transport._connection and
doc/SECURITY.md §7 / WS_PROTOCOL.md §8).

Also covers `request_shutdown` — the client-requested stop backing the
`server.shutdown` command (WS_PROTOCOL.md §7.6g).

Uses a duck-typed fake manager/session rather than a real SessionManager —
ConnectionRegistry only ever calls `manager.session_for_connection(conn.id)`
and reads `session.channel`, so a full engine/gateway stack would be
incidental weight here.
"""

from __future__ import annotations

import asyncio
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


# ---------------------------------------------------------------------------
# request_shutdown — the `server.shutdown` command's trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_shutdown_invokes_the_stop_callback() -> None:
    stopped: list[bool] = []
    registry = ConnectionRegistry(_FakeManager())  # type: ignore[arg-type]
    # A grace period long enough that the idle self-reap can never be what
    # fires here — only request_shutdown can.
    registry.set_idle_shutdown(lambda: stopped.append(True), 3600.0)

    registry.request_shutdown("py-kodo upgrade")

    assert stopped == [], "must not fire synchronously — the ack has to leave the socket first"
    await asyncio.sleep(0.3)
    assert stopped == [True]


@pytest.mark.asyncio
async def test_request_shutdown_without_a_stop_callback_is_a_no_op() -> None:
    """Nothing wires a stop callback outside `kodo.server.__main__` (tests and
    the validator's in-process app included), so this must not raise."""
    registry = ConnectionRegistry(_FakeManager())  # type: ignore[arg-type]

    registry.request_shutdown("no callback set")

    await asyncio.sleep(0.3)

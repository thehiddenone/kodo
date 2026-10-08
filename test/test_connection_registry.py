"""Behavior tests for kodo.server.ConnectionRegistry's frame dispatch.

Focused on `kind="response"` routing: a client's answer to a server-initiated
request (approval/question/permission/API key) must resolve the future on
the *session's* SessionChannel — found via the connection it arrived on —
not (as before this fix) on the Connection object itself, which no longer
owns any pending-future state at all (see kodo.transport._connection and
doc/SECURITY.md §7 / WS_PROTOCOL.md §8).

Also covers `request_shutdown` — the client-requested stop backing the
`server.shutdown` command (WS_PROTOCOL.md §7.6g) — and the shutdown latch
(`begin_shutdown` / `close_connections`) that keeps windows from attaching to a
server that is going away.

Uses a duck-typed fake manager/session rather than a real SessionManager —
ConnectionRegistry only ever calls `manager.session_for_connection(conn.id)`
and reads `session.channel`, so a full engine/gateway stack would be
incidental weight here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import WSCloseCode, WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from kodo.common import Envelope
from kodo.server import SERVER_STATE_HEADER, ConnectionRegistry, Request
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
    """Nothing wires a stop callback outside `kodo.server.__main__` (the tests'
    in-process apps included), so this must not raise."""
    registry = ConnectionRegistry(_FakeManager())  # type: ignore[arg-type]

    registry.request_shutdown("no callback set")

    await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# run_ws — the live socket loop, driven through an in-process loopback server
# ---------------------------------------------------------------------------


_TIMEOUT = 2.0


class _LiveManager:
    """Duck-typed SessionManager covering every call run_ws makes."""

    def __init__(self, sessions: dict[str, object] | None = None) -> None:
        self.sessions = sessions or {}
        self.dropped: list[str] = []
        self.running = False

    def session_for_connection(self, _conn_id: str) -> object | None:
        return None

    def get(self, session_id: str) -> object | None:
        return self.sessions.get(session_id)

    def drop_connection(self, conn: Connection) -> None:
        self.dropped.append(conn.id)

    def any_running(self) -> bool:
        return self.running


async def _wait_until(predicate: Callable[[], bool], timeout: float = _TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


@asynccontextmanager
async def _serve(registry: ConnectionRegistry) -> AsyncIterator[TestClient]:
    app = web.Application()
    app.router.add_get("/ws", registry.run_ws)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def _recv(ws: object) -> Envelope:
    raw = await ws.receive_str(timeout=_TIMEOUT)  # type: ignore[attr-defined]
    return Envelope.from_json(raw)


def test_manager_property_returns_the_injected_manager() -> None:
    manager = _LiveManager()
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]
    assert registry.manager is manager


@pytest.mark.asyncio
async def test_handler_receives_a_request_with_the_resolved_session() -> None:
    session = SimpleNamespace(name="the-session")
    manager = _LiveManager({"s-1": session})
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]
    seen: list[tuple[object, str, object]] = []

    async def _handler(req: Request) -> None:
        seen.append((req.session, req.session_id, req.manager))
        await req.reply({"type": "pong", "session_id": req.session_id})

    registry.register_handler("ping", _handler)

    async with _serve(registry) as client, client.ws_connect("/ws") as ws:
        with_session = Envelope(kind="request", payload={"type": "ping", "session_id": "s-1"})
        await ws.send_str(with_session.to_json())
        first = await _recv(ws)
        unknown = Envelope(kind="request", payload={"type": "ping", "session_id": "nope"})
        await ws.send_str(unknown.to_json())
        await _recv(ws)
        no_session = Envelope(kind="request", payload={"type": "ping"})
        await ws.send_str(no_session.to_json())
        third = await _recv(ws)

    assert first.correlation_id == with_session.id
    assert first.payload == {"type": "pong", "session_id": "s-1"}
    assert third.payload == {"type": "pong", "session_id": ""}
    assert seen == [(session, "s-1", manager), (None, "nope", manager), (None, "", manager)]


@pytest.mark.asyncio
async def test_unknown_message_type_and_malformed_frames() -> None:
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async with _serve(registry) as client, client.ws_connect("/ws") as ws:
        await ws.send_str("not json at all")
        await ws.send_str('{"payload": {}}')  # no "kind"
        request = Envelope(kind="request", payload={"type": "bogus"})
        await ws.send_str(request.to_json())
        reply = await _recv(ws)

    # The first frame back answers the valid request; the malformed ones got nothing.
    assert reply.correlation_id == request.id
    assert reply.payload["type"] == "error"
    assert reply.payload["code"] == "unknown_message"
    assert reply.payload["recoverable"] is True


@pytest.mark.asyncio
async def test_control_connection_response_resolves_on_the_connection() -> None:
    """A response on a connection bound to no session resolves the future the
    server registered on that Connection itself; a disconnect cancels any
    future still pending there."""
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[dict[str, object]] = loop.create_future()
    abandoned: asyncio.Future[dict[str, object]] = loop.create_future()

    async def _handler(req: Request) -> None:
        req.connection.register_response_future("ask-1", answered)
        req.connection.register_response_future("ask-2", abandoned)
        await req.reply({"type": "ok"})

    registry.register_handler("setup", _handler)

    async with _serve(registry) as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "setup"}).to_json())
            await _recv(ws)
            answer = Envelope(kind="response", correlation_id="ask-1", payload={"key": "v"})
            await ws.send_str(answer.to_json())
            result = await asyncio.wait_for(answered, _TIMEOUT)
        await _wait_until(abandoned.done)

    assert result == {"key": "v"}
    assert abandoned.cancelled()


@pytest.mark.asyncio
async def test_disconnect_detaches_the_connection_from_the_manager() -> None:
    manager = _LiveManager()
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]
    conn_ids: list[str] = []

    async def _handler(req: Request) -> None:
        conn_ids.append(req.connection.id)
        await req.reply({"type": "ok"})

    registry.register_handler("hello", _handler)

    async with _serve(registry) as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "hello"}).to_json())
            await _recv(ws)
            assert manager.dropped == []
        await _wait_until(lambda: bool(manager.dropped))

    assert manager.dropped == conn_ids


# ---------------------------------------------------------------------------
# Idle self-reap and GPU release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_shutdown_fires_only_after_the_last_window_leaves() -> None:
    stopped: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async with _serve(registry) as client:
        registry.set_idle_shutdown(lambda: stopped.append(True), 0.05)
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
            await _recv(ws)  # the connection is registered server-side
            await asyncio.sleep(0.1)  # longer than the grace — must not fire
            assert stopped == []
        await _wait_until(lambda: bool(stopped))

    assert stopped == [True]


@pytest.mark.asyncio
async def test_idle_shutdown_with_no_connection_ever_still_fires() -> None:
    stopped: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    registry.set_idle_shutdown(lambda: stopped.append(True), 0.01)

    await _wait_until(lambda: bool(stopped))
    assert stopped == [True]


@pytest.mark.asyncio
async def test_idle_shutdown_is_deferred_while_a_turn_is_running() -> None:
    stopped: list[bool] = []
    manager = _LiveManager()
    manager.running = True
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    registry.set_idle_shutdown(lambda: stopped.append(True), 0.02)
    await asyncio.sleep(0.1)  # several grace periods elapse
    assert stopped == []

    manager.running = False
    await _wait_until(lambda: bool(stopped))
    assert stopped == [True]


@pytest.mark.asyncio
async def test_request_shutdown_cancels_a_pending_idle_reap() -> None:
    stopped: list[str] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]
    registry.set_idle_shutdown(lambda: stopped.append("stop"), 0.05)

    registry.request_shutdown("explicit")

    await _wait_until(lambda: bool(stopped))
    await asyncio.sleep(0.1)  # past the idle grace — a stray reap would show up
    # Exactly one stop: the explicit request, not also the idle reap.
    assert stopped == ["stop"]


@pytest.mark.asyncio
async def test_gpu_released_when_the_last_window_leaves() -> None:
    released: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async def _release() -> None:
        released.append(True)

    registry.set_gpu_release_hook(_release)

    async with _serve(registry) as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
            await _recv(ws)
            assert released == []
        await _wait_until(lambda: bool(released))

    assert released == [True]


@pytest.mark.asyncio
async def test_gpu_kept_while_a_turn_is_still_running() -> None:
    released: list[bool] = []
    manager = _LiveManager()
    manager.running = True
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    async def _release() -> None:
        released.append(True)

    registry.set_gpu_release_hook(_release)

    async with _serve(registry) as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
            await _recv(ws)
        await _wait_until(lambda: bool(manager.dropped))
        await asyncio.sleep(0.05)  # let the scheduled release task run

    assert released == []


@pytest.mark.asyncio
async def test_idle_shutdown_armed_while_connected_does_not_reap() -> None:
    """Arming the idle reap while a window is already connected must not stop
    the server out from under that window."""
    stopped: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async with _serve(registry) as client:
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
            await _recv(ws)
            registry.set_idle_shutdown(lambda: stopped.append(True), 0.01)
            await asyncio.sleep(0.05)
            assert stopped == []
        await _wait_until(lambda: bool(stopped))


@pytest.mark.asyncio
async def test_protocol_error_drops_the_connection() -> None:
    """A frame over aiohttp's default 4 MiB limit is a protocol error; the
    socket is torn down and the manager is told the connection is gone."""
    manager = _LiveManager()
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    async with _serve(registry) as client:
        async with client.ws_connect("/ws", max_msg_size=0) as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
            await _recv(ws)
            with contextlib.suppress(OSError):
                await ws.send_str("x" * (4 * 1024 * 1024 + 1))
                await ws.receive(timeout=_TIMEOUT)
        await _wait_until(lambda: bool(manager.dropped))

    assert len(manager.dropped) == 1


# ---------------------------------------------------------------------------
# Shutdown latch — begin_shutdown / close_connections
# ---------------------------------------------------------------------------


async def _assert_refused_as_stopping(client: TestClient) -> None:
    with pytest.raises(WSServerHandshakeError) as excinfo:
        await client.ws_connect("/ws")
    assert excinfo.value.status == 503
    assert excinfo.value.headers is not None
    assert excinfo.value.headers.get(SERVER_STATE_HEADER) == "stopping"


@pytest.mark.asyncio
async def test_new_connections_are_refused_once_shutdown_is_committed() -> None:
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]
    assert registry.stopping is False

    async with _serve(registry) as client:
        registry.begin_shutdown()
        assert registry.stopping is True
        await _assert_refused_as_stopping(client)


@pytest.mark.asyncio
async def test_request_shutdown_refuses_new_connections_before_it_stops() -> None:
    """Between the ack and the actual stop, a newcomer must not get a socket
    that is about to be torn down under it."""
    stopped: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async with _serve(registry) as client:
        registry.set_idle_shutdown(lambda: stopped.append(True), 3600.0)
        registry.request_shutdown("upgrade")
        await _assert_refused_as_stopping(client)
        assert stopped == []


@pytest.mark.asyncio
async def test_idle_reap_refuses_connections_arriving_after_it_fired() -> None:
    stopped: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async with _serve(registry) as client:
        registry.set_idle_shutdown(lambda: stopped.append(True), 0.01)
        await _wait_until(lambda: bool(stopped))
        await _assert_refused_as_stopping(client)


@pytest.mark.asyncio
async def test_close_connections_closes_open_sockets_as_going_away() -> None:
    manager = _LiveManager()
    registry = ConnectionRegistry(manager)  # type: ignore[arg-type]

    async with _serve(registry) as client, client.ws_connect("/ws") as ws:
        await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
        await _recv(ws)  # the connection is registered server-side

        await asyncio.wait_for(registry.close_connections(), _TIMEOUT)

        msg = await ws.receive(timeout=_TIMEOUT)
        assert msg.type == WSMsgType.CLOSE
        assert ws.close_code == WSCloseCode.GOING_AWAY
        await _wait_until(lambda: bool(manager.dropped))
        assert registry.stopping is True


@pytest.mark.asyncio
async def test_a_window_leaving_a_stopping_server_does_not_rearm_the_reap() -> None:
    """Once the shutdown is committed, the last window leaving must not start
    a second, independent stop."""
    stopped: list[bool] = []
    released: list[bool] = []
    registry = ConnectionRegistry(_LiveManager())  # type: ignore[arg-type]

    async def _release() -> None:
        released.append(True)

    registry.set_gpu_release_hook(_release)

    async with _serve(registry) as client:
        registry.set_idle_shutdown(lambda: stopped.append(True), 0.02)
        async with client.ws_connect("/ws") as ws:
            await ws.send_str(Envelope(kind="request", payload={"type": "x"}).to_json())
            await _recv(ws)
            registry.begin_shutdown()
        await asyncio.sleep(0.1)  # several grace periods

    assert stopped == []
    assert released == []

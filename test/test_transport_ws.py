"""Behavior tests for kodo.transport.WebSocketDispatcher's live socket loop.

Drives ``run_ws`` end to end through an in-process aiohttp test server bound
to loopback: request frames dispatch by ``payload.type``, response frames
resolve server-initiated request futures, buffered frames drain on connect,
and a disconnect cancels every still-pending future.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kodo.common import Envelope
from kodo.transport import Outbox, WebSocketDispatcher

_RECEIVE_TIMEOUT = 2.0


async def _wait_until(predicate: object, timeout: float = _RECEIVE_TIMEOUT) -> None:
    assert callable(predicate)
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


@pytest.fixture
async def dispatcher() -> WebSocketDispatcher:
    return WebSocketDispatcher(Outbox())


@pytest.fixture
async def client(dispatcher: WebSocketDispatcher) -> AsyncGenerator[TestClient, None]:
    app = web.Application()
    app.router.add_get("/ws", dispatcher.run_ws)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def _recv(ws: object) -> Envelope:
    raw = await ws.receive_str(timeout=_RECEIVE_TIMEOUT)  # type: ignore[attr-defined]
    return Envelope.from_json(raw)


# ---------------------------------------------------------------------------
# kind=request dispatch
# ---------------------------------------------------------------------------


async def test_request_frame_is_routed_to_its_registered_handler(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    seen_ws: list[bool] = []

    async def _ping(state: WebSocketDispatcher, env: Envelope) -> None:
        seen_ws.append(state.ws is not None)
        await state.send(Envelope.make_response(env.id, {"type": "pong"}))

    dispatcher.register_handler("ping", _ping)

    async with client.ws_connect("/ws") as ws:
        request = Envelope(kind="request", payload={"type": "ping"})
        await ws.send_str(request.to_json())
        reply = await _recv(ws)

    assert reply.kind == "response"
    assert reply.correlation_id == request.id
    assert reply.payload == {"type": "pong"}
    assert seen_ws == [True]


async def test_unknown_message_type_gets_a_recoverable_error_response(
    client: TestClient,
) -> None:
    async with client.ws_connect("/ws") as ws:
        request = Envelope(kind="request", payload={"type": "no.such.thing"})
        await ws.send_str(request.to_json())
        reply = await _recv(ws)

    assert reply.correlation_id == request.id
    assert reply.payload["type"] == "error"
    assert reply.payload["code"] == "unknown_message"
    assert reply.payload["recoverable"] is True
    assert "no.such.thing" in str(reply.payload["message"])


async def test_malformed_frames_are_ignored_and_the_socket_stays_usable(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    async def _ping(state: WebSocketDispatcher, env: Envelope) -> None:
        await state.send(Envelope.make_response(env.id, {"type": "pong"}))

    dispatcher.register_handler("ping", _ping)

    async with client.ws_connect("/ws") as ws:
        await ws.send_str("this is not json")
        await ws.send_str('{"payload": {"type": "ping"}}')  # missing "kind"
        request = Envelope(kind="request", payload={"type": "ping"})
        await ws.send_str(request.to_json())
        reply = await _recv(ws)

    # The first reply is for the valid frame — nothing was sent for the bad ones.
    assert reply.correlation_id == request.id
    assert reply.payload == {"type": "pong"}


# ---------------------------------------------------------------------------
# kind=response resolution
# ---------------------------------------------------------------------------


async def test_response_frame_resolves_the_registered_future(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    dispatcher.register_response_future("req-1", future)

    async with client.ws_connect("/ws") as ws:
        answer = Envelope(kind="response", correlation_id="req-1", payload={"action": "allow"})
        await ws.send_str(answer.to_json())
        result = await asyncio.wait_for(future, _RECEIVE_TIMEOUT)

    assert result == {"action": "allow"}


async def test_unmatched_or_uncorrelated_responses_are_dropped_silently(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    dispatcher.register_response_future("req-1", future)

    async with client.ws_connect("/ws") as ws:
        await ws.send_str(
            Envelope(kind="response", correlation_id="other", payload={"x": 1}).to_json()
        )
        await ws.send_str(Envelope(kind="response", payload={"x": 2}).to_json())
        await ws.send_str(
            Envelope(kind="response", correlation_id="req-1", payload={"x": 3}).to_json()
        )
        result = await asyncio.wait_for(future, _RECEIVE_TIMEOUT)
        # A duplicate answer for an already-resolved id is dropped, not a crash,
        # and never produces a reply frame.
        await ws.send_str(
            Envelope(kind="response", correlation_id="req-1", payload={"x": 4}).to_json()
        )
        probe = Envelope(kind="request", payload={"type": "probe"})
        await ws.send_str(probe.to_json())
        reply = await _recv(ws)

    assert result == {"x": 3}
    assert reply.correlation_id == probe.id


async def test_discarded_response_future_is_not_resolved(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    dispatcher.register_response_future("req-1", future)
    dispatcher.discard_response_future("req-1")
    dispatcher.discard_response_future("never-registered")  # no-op

    async with client.ws_connect("/ws") as ws:
        await ws.send_str(
            Envelope(kind="response", correlation_id="req-1", payload={"x": 1}).to_json()
        )
        # Round-trip a request so the response frame has certainly been processed.
        probe = Envelope(kind="request", payload={"type": "probe"})
        await ws.send_str(probe.to_json())
        await _recv(ws)
        assert not future.done()


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


async def test_frames_buffered_while_disconnected_are_drained_on_connect(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    buffered = Envelope.make_event("state", {"phase": "idle"})
    await dispatcher.send(buffered)
    assert dispatcher.outbox.pending == 1

    async with client.ws_connect("/ws") as ws:
        received = await _recv(ws)

    assert received.id == buffered.id
    assert dispatcher.outbox.pending == 0


async def test_disconnect_cancels_pending_futures_and_clears_the_socket(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()

    async with client.ws_connect("/ws"):
        await _wait_until(lambda: dispatcher.ws is not None)
        dispatcher.register_response_future("req-1", future)

    await _wait_until(lambda: dispatcher.ws is None)
    assert future.cancelled()

    # Afterwards, sends buffer rather than fail.
    await dispatcher.send(Envelope.make_event("state", {}))
    assert dispatcher.outbox.pending == 1


async def test_disconnect_leaves_already_resolved_futures_untouched(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    future.set_result({"done": True})

    async with client.ws_connect("/ws"):
        await _wait_until(lambda: dispatcher.ws is not None)
        dispatcher.register_response_future("req-1", future)

    await _wait_until(lambda: dispatcher.ws is None)
    assert future.result() == {"done": True}


async def test_new_connection_replaces_and_closes_the_previous_one(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    async def _ping(state: WebSocketDispatcher, env: Envelope) -> None:
        await state.send(Envelope.make_response(env.id, {"type": "pong"}))

    dispatcher.register_handler("ping", _ping)

    first = await client.ws_connect("/ws")
    await _wait_until(lambda: dispatcher.ws is not None)
    first_server_ws = dispatcher.ws

    async with client.ws_connect("/ws") as second:
        await _wait_until(lambda: dispatcher.ws is not first_server_ws)
        # The superseded client observes a close.
        msg = await first.receive(timeout=_RECEIVE_TIMEOUT)
        assert msg.type.name in {"CLOSE", "CLOSING", "CLOSED"}

        # The replacement connection is the one replies go to.
        request = Envelope(kind="request", payload={"type": "ping"})
        await second.send_str(request.to_json())
        reply = await _recv(second)
        assert reply.correlation_id == request.id

    await first.close()


async def test_replaced_connection_cleaning_up_late_leaves_the_new_one_intact(
    dispatcher: WebSocketDispatcher, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A superseded connection that is mid-dispatch only finishes its cleanup
    after the replacement is installed — that late cleanup must neither detach
    the new socket nor cancel requests made on it."""
    caplog.set_level(logging.INFO)
    release = asyncio.Event()

    async def _hold(state: WebSocketDispatcher, env: Envelope) -> None:
        await release.wait()

    dispatcher.register_handler("hold", _hold)

    first = await client.ws_connect("/ws")
    await _wait_until(lambda: dispatcher.ws is not None)
    first_server_ws = dispatcher.ws
    await first.send_str(Envelope(kind="request", payload={"type": "hold"}).to_json())
    stale = asyncio.get_running_loop().create_future()
    dispatcher.register_response_future("sent-to-first", stale)

    async with client.ws_connect("/ws") as second:
        msg = await first.receive(timeout=_RECEIVE_TIMEOUT)  # answers the server's close
        assert msg.type.name in {"CLOSE", "CLOSING", "CLOSED"}
        await _wait_until(lambda: dispatcher.ws not in (None, first_server_ws))
        second_server_ws = dispatcher.ws
        assert stale.cancelled()  # the replaced client can no longer answer it

        fresh = asyncio.get_running_loop().create_future()
        dispatcher.register_response_future("sent-to-second", fresh)
        release.set()
        await _wait_until(lambda: "WebSocket disconnected" in caplog.text)

        assert dispatcher.ws is second_server_ws
        assert not fresh.cancelled()
        await second.send_str(Envelope.make_response("sent-to-second", {"ok": True}).to_json())
        assert await asyncio.wait_for(fresh, _RECEIVE_TIMEOUT) == {"ok": True}

    await first.close()


async def test_protocol_error_ends_the_connection_cleanly(
    dispatcher: WebSocketDispatcher, client: TestClient
) -> None:
    """A frame over aiohttp's default 4 MiB limit is a protocol error: the
    server logs it and tears the connection down, cancelling pending futures."""
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()

    async with client.ws_connect("/ws", max_msg_size=0) as ws:
        await _wait_until(lambda: dispatcher.ws is not None)
        dispatcher.register_response_future("req-1", future)
        # The server may reset the socket mid-write; either way it has seen
        # the oversized frame.
        with contextlib.suppress(OSError):
            await ws.send_str("x" * (4 * 1024 * 1024 + 1))
            await ws.receive(timeout=_RECEIVE_TIMEOUT)

    await _wait_until(lambda: dispatcher.ws is None)
    assert future.cancelled()

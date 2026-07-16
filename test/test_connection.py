"""Behavior tests for kodo.transport._connection: Connection and SessionChannel.

Covers the pending-response-future ownership model — futures and their
originating request envelopes live on the session-scoped SessionChannel, not
the socket-scoped Connection, so a disconnect/reconnect (unlike genuine
session teardown) never loses a still-outstanding approval/question/
permission/API-key prompt. See doc/SECURITY.md §7 and WS_PROTOCOL.md §8.
"""

from __future__ import annotations

import asyncio

import pytest

from kodo.common import Envelope
from kodo.transport import Connection, SessionChannel


class _FakeWS:
    """Minimal stand-in for an aiohttp WebSocketResponse."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


def _conn() -> tuple[Connection, _FakeWS]:
    ws = _FakeWS()
    return Connection(ws), ws  # type: ignore[arg-type]


def _request_env(req_id: str = "req-1") -> Envelope:
    return Envelope(kind="request", id=req_id, payload={"type": "prompt.permission"})


# ---------------------------------------------------------------------------
# register_response_future / resolve_response — session-scoped, not conn-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_response_sets_the_registered_future() -> None:
    channel = SessionChannel()
    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()
    channel.register_response_future("req-1", future)

    channel.resolve_response("req-1", {"action": "allow"})

    assert future.done()
    assert future.result() == {"action": "allow"}


def test_resolve_response_with_unknown_correlation_id_is_a_noop() -> None:
    channel = SessionChannel()
    # Must not raise — a late/duplicate response after the future was already
    # popped (or never registered) is logged and dropped.
    channel.resolve_response("no-such-id", {"action": "allow"})


@pytest.mark.asyncio
async def test_register_response_future_does_not_require_a_live_connection() -> None:
    """Unlike the old Connection-owned model, registering (and later
    resolving) a future never needs an attached connection at all — it is
    purely a SessionChannel-local map."""
    channel = SessionChannel()
    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()

    channel.register_response_future("req-1", future)  # no attach() call
    channel.resolve_response("req-1", {"action": "deny"})

    assert future.result() == {"action": "deny"}


@pytest.mark.asyncio
async def test_disconnect_then_reconnect_does_not_lose_a_pending_future() -> None:
    """The scenario this whole model exists for: a request is sent while
    connected, the socket then drops (detach, no cancellation — that is the
    connection registry's job, exercised in test_connection_registry.py /
    test_session_manager.py, not here), and the eventual response — arriving
    over a brand-new Connection — still resolves the original future."""
    channel = SessionChannel()
    conn1, ws1 = _conn()
    await channel.attach(conn1)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()
    channel.register_response_future("req-1", future)
    await channel.send(_request_env("req-1"))
    assert ws1.sent  # delivered live, not buffered

    # Simulate a disconnect: nothing cancels the future (that's the point).
    channel.detach(conn1)
    assert not future.done()

    # Reconnect on a fresh Connection/socket.
    conn2, _ws2 = _conn()
    await channel.attach(conn2)

    channel.resolve_response("req-1", {"action": "allow"})
    assert future.result() == {"action": "allow"}


# ---------------------------------------------------------------------------
# send() remembers kind="request" envelopes for replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_request_envelope_is_tracked_for_replay() -> None:
    channel = SessionChannel()
    conn, ws = _conn()
    await channel.attach(conn)

    await channel.send(_request_env("req-1"))
    ws.sent.clear()

    conn2, ws2 = _conn()
    await channel.attach(conn2)
    await channel.replay_pending_requests()

    assert len(ws2.sent) == 1


@pytest.mark.asyncio
async def test_send_event_envelope_is_not_tracked_for_replay() -> None:
    """Only kind='request' round-trips get replayed — an ordinary event
    (stream chunk, state update, ...) is not a prompt awaiting an answer."""
    channel = SessionChannel()
    conn, _ws = _conn()
    await channel.attach(conn)

    await channel.send(Envelope.make_event("state", {}))

    conn2, ws2 = _conn()
    await channel.attach(conn2)
    await channel.replay_pending_requests()

    assert ws2.sent == []


@pytest.mark.asyncio
async def test_replay_pending_requests_skips_already_resolved_ones() -> None:
    channel = SessionChannel()
    conn, _ws = _conn()
    await channel.attach(conn)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()
    channel.register_response_future("req-1", future)
    await channel.send(_request_env("req-1"))

    channel.resolve_response("req-1", {"action": "allow"})

    conn2, ws2 = _conn()
    await channel.attach(conn2)
    await channel.replay_pending_requests()

    assert ws2.sent == []


@pytest.mark.asyncio
async def test_replay_pending_requests_resends_with_the_same_request_id() -> None:
    """The replayed envelope must carry the original request id, so whichever
    connection eventually delivers the client's answer still resolves the
    same future via that id."""
    channel = SessionChannel()
    conn, _ws = _conn()
    await channel.attach(conn)

    await channel.send(_request_env("req-1"))

    conn2, ws2 = _conn()
    await channel.attach(conn2)
    await channel.replay_pending_requests()

    replayed = Envelope.from_json(ws2.sent[0])
    assert replayed.id == "req-1"
    assert replayed.kind == "request"


@pytest.mark.asyncio
async def test_replay_pending_requests_noop_when_not_attached() -> None:
    channel = SessionChannel()
    conn, _ws = _conn()
    await channel.attach(conn)
    await channel.send(_request_env("req-1"))
    channel.detach(conn)

    # Must not raise even though nothing is attached to replay onto.
    await channel.replay_pending_requests()


@pytest.mark.asyncio
async def test_multiple_pending_requests_all_replayed() -> None:
    channel = SessionChannel()
    conn, _ws = _conn()
    await channel.attach(conn)
    await channel.send(_request_env("req-1"))
    await channel.send(_request_env("req-2"))

    conn2, ws2 = _conn()
    await channel.attach(conn2)
    await channel.replay_pending_requests()

    replayed_ids = {Envelope.from_json(raw).id for raw in ws2.sent}
    assert replayed_ids == {"req-1", "req-2"}

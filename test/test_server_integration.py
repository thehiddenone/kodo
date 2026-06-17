"""Integration tests: start the kodo server and communicate via WebSocket.

Fixtures start a real aiohttp server (in-process, random port) and connect
via a WebSocket client.  No LLM calls are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.server import Config, create_app

_RECV_TIMEOUT = 5.0  # seconds per frame


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Minimal Kodo project directory with a valid kodo.md."""
    (tmp_path / "kodo.md").write_text(
        "# Kodo Project\n\n> Project marker.\n\n## Toolchain\n\n- python\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
async def server(project_dir: Path) -> AsyncGenerator[TestServer, None]:
    """Start a kodo server bound to a random loopback port."""
    config = Config(project=project_dir)
    app = create_app(config)
    srv = TestServer(app)
    await srv.start_server()
    yield srv
    await srv.close()


@pytest.fixture
async def ws(server: TestServer) -> AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
    """Open a WebSocket connection to the running server."""
    session = aiohttp.ClientSession()
    conn = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    yield conn
    await conn.close()
    await session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _recv(ws: aiohttp.ClientWebSocketResponse, timeout: float = _RECV_TIMEOUT) -> Envelope:
    """Receive the next frame and parse it as an Envelope."""
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    assert msg.type == aiohttp.WSMsgType.TEXT, f"Expected TEXT frame, got {msg.type}"
    return Envelope.from_json(str(msg.data))


async def _recv_response(
    ws: aiohttp.ClientWebSocketResponse,
    correlation_id: str,
    timeout: float = _RECV_TIMEOUT,
) -> Envelope:
    """Drain frames until the response matching correlation_id arrives."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"No response for id={correlation_id!r} within {timeout}s")
        env = await _recv(ws, timeout=remaining)
        if env.kind == "response" and env.correlation_id == correlation_id:
            return env


def _make_request(msg_type: str, **payload: object) -> Envelope:
    """Build a request envelope for the given message type."""
    return Envelope(kind="request", payload={"type": msg_type, **payload})


# ---------------------------------------------------------------------------
# hello
# ---------------------------------------------------------------------------


async def test_hello_returns_server_version(
    ws: aiohttp.ClientWebSocketResponse, project_dir: Path
) -> None:
    """
    Given a connected WebSocket client,
    when a hello request is sent,
    then the response carries server_version and project_root.
    """
    req = _make_request("hello", client="vsix", version="0.1.0")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "hello.ack"
    assert resp.payload["server_version"] == "0.1.0b1"
    assert str(project_dir) == str(resp.payload["project_root"])


async def test_hello_ack_embeds_state_snapshot(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a hello request is sent,
    then the hello.ack response embeds a state snapshot (WS_PROTOCOL.md §4.1).
    """
    req = _make_request("hello", client="vsix", version="0.1.0")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert "state" in resp.payload
    state = resp.payload["state"]
    assert isinstance(state, dict)
    assert "phase" in state


async def test_hello_triggers_state_event(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a hello request is sent,
    then a state event is broadcast (alongside the hello response).
    """
    req = _make_request("hello", client="vsix", version="0.1.0")
    await ws.send_str(req.to_json())

    received: list[Envelope] = []
    for _ in range(4):
        try:
            env = await _recv(ws, timeout=2.0)
            received.append(env)
            if any(e.kind == "event" and e.payload.get("type") == "state" for e in received):
                break
        except TimeoutError:
            break

    state_events = [e for e in received if e.kind == "event" and e.payload.get("type") == "state"]
    assert len(state_events) == 1


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


async def test_ping_returns_pong(ws: aiohttp.ClientWebSocketResponse) -> None:
    """
    Given a connected WebSocket client,
    when a ping request is sent,
    then the server responds with type='pong'.
    """
    req = _make_request("ping")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "pong"


async def test_ping_response_correlates_to_request(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a ping request with a known id,
    when the server responds,
    then the correlation_id matches the request id.
    """
    req = _make_request("ping")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.correlation_id == req.id


async def test_multiple_sequential_pings(ws: aiohttp.ClientWebSocketResponse) -> None:
    """
    Given a connected client,
    when two ping requests are sent sequentially,
    then each response correlates to its own request.
    """
    req_a = _make_request("ping")
    req_b = _make_request("ping")

    await ws.send_str(req_a.to_json())
    resp_a = await _recv_response(ws, req_a.id)

    await ws.send_str(req_b.to_json())
    resp_b = await _recv_response(ws, req_b.id)

    assert resp_a.correlation_id == req_a.id
    assert resp_b.correlation_id == req_b.id


# ---------------------------------------------------------------------------
# unknown message type
# ---------------------------------------------------------------------------


async def test_unknown_message_returns_error(ws: aiohttp.ClientWebSocketResponse) -> None:
    """
    Given a connected client,
    when a message with an unknown type is sent,
    then the server responds with type='error' and code='unknown_message'.
    """
    req = _make_request("does.not.exist")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "unknown_message"


# ---------------------------------------------------------------------------
# prompt.submit
# ---------------------------------------------------------------------------


async def test_prompt_submit_with_text_returns_accepted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a prompt.submit request with non-empty text is sent,
    then the server responds with type='prompt.accepted'.
    """
    req = _make_request("prompt.submit", text="Build me a trading bot.")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "prompt.accepted"


async def test_prompt_submit_with_empty_text_returns_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a prompt.submit request with empty text is sent,
    then the server responds with type='error' and code='empty_prompt'.
    """
    req = _make_request("prompt.submit", text="")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "empty_prompt"


# ---------------------------------------------------------------------------
# mode.set
# ---------------------------------------------------------------------------


async def test_mode_set_autonomous_returns_accepted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a mode.set request with autonomous=True is sent,
    then the server responds with type='mode.accepted'.
    """
    req = _make_request("mode.set", autonomous=True)
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "mode.accepted"


# ---------------------------------------------------------------------------
# approval.respond was removed (WS_PROTOCOL.md §6.2)
# Gates now use kind=request / kind=response with correlation_id.
# ---------------------------------------------------------------------------


async def test_approval_respond_is_now_unknown_message(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when the old approval.respond message type is sent,
    then the server returns unknown_message (the handler was removed).
    """
    req = _make_request("approval.respond", gate_id="x", action="agree")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "unknown_message"


async def test_kind_response_with_no_pending_future_is_silently_dropped(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a kind=response frame arrives with a correlation_id that has no
    pending future registered,
    then the server silently drops it (no error, no crash).
    """
    orphan = Envelope(
        kind="response",
        correlation_id="no-such-request",
        payload={"action": "agree"},
    )
    await ws.send_str(orphan.to_json())

    # Send a ping afterwards; if the server crashed or errored on the orphan
    # response we would not get a pong back.
    ping = _make_request("ping")
    await ws.send_str(ping.to_json())
    resp = await _recv_response(ws, ping.id)
    assert resp.payload["type"] == "pong"


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


async def test_stop_returns_accepted(ws: aiohttp.ClientWebSocketResponse) -> None:
    """
    Given a connected client,
    when a stop request is sent,
    then the server responds with type='stop.accepted'.
    """
    req = _make_request("stop")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "stop.accepted"


# ---------------------------------------------------------------------------
# session.resume was removed — resume is now automatic on bootstrap
# (STATE_AND_LIFECYCLE.md §3 Phase 4; STATE_AND_LIFECYCLE.md §7)

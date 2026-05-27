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

from kodo.server._app import create_app
from kodo.server._config import Config
from kodo.transport._envelope import Envelope

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

    assert resp.payload["type"] == "hello"
    assert resp.payload["server_version"] == "0.1.0b1"
    assert str(project_dir) == str(resp.payload["project_root"])


async def test_hello_fresh_project_has_no_last_session(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a fresh project with no prior sessions,
    when a hello request is sent,
    then last_session in the response is None.
    """
    req = _make_request("hello", client="vsix", version="0.1.0")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["last_session"] is None


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
# approval.respond
# ---------------------------------------------------------------------------


async def test_approval_respond_with_empty_gate_id_returns_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when an approval.respond request with empty gate_id is sent,
    then the server responds with type='error' and code='missing_gate_id'.
    """
    req = _make_request("approval.respond", gate_id="", action="agree", feedback="")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "missing_gate_id"


async def test_approval_respond_with_unknown_gate_id_returns_accepted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when an approval.respond request with a gate_id that has no pending gate is sent,
    then the server responds with type='approval.accepted' (stale gate_id is logged
    and accepted gracefully).
    """
    req = _make_request("approval.respond", gate_id="no-such-gate", action="agree", feedback="")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "approval.accepted"


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
# session.resume
# ---------------------------------------------------------------------------


async def test_session_resume_with_unknown_session_returns_accepted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """
    Given a connected client,
    when a session.resume request is sent for an unknown session_id,
    then the server responds with type='session.resume.accepted' (the engine
    emits an error event for the missing prompt, then the handler responds).
    """
    req = _make_request("session.resume", session_id="nonexistent-session-xyz")
    await ws.send_str(req.to_json())

    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "session.resume.accepted"

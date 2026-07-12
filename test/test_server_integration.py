"""Integration tests: start the singleton kodo server and talk to it over WS.

Fixtures start a real aiohttp server (in-process, random port) with ``HOME``
redirected to a temp dir so the real ``~/.kodo`` is never touched.  Every frame
except ``hello`` carries a ``session_id``; ``hello`` mints (or resumes) one.
No LLM calls are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.runtime._engine import _titling as _titling_module
from kodo.server import Config, create_app
from kodo.server import _app as _app_module

_RECV_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # _start_background best-effort-warms the titler model cache on startup
    # (doc/INTERNALS.md §10c); stubbed here so server boot stays fully
    # offline/deterministic like the rest of this file (see module docstring)
    # instead of racing a real HuggingFace download every test.
    monkeypatch.setattr(_app_module, "warm_up_titler_cache", lambda: None)
    # SessionTitler fires kodo.titling.generate_title fire-and-forget on the
    # first prompt of every session (runtime/_engine/_titling.py) — stubbed
    # here too, otherwise any test that submits a real prompt triggers a real
    # HuggingFace download/model load in the background, same as above.
    monkeypatch.setattr(_titling_module, "generate_title", lambda text: None)
    return tmp_path


@pytest.fixture
async def server() -> AsyncGenerator[TestServer, None]:
    app = create_app(Config())
    srv = TestServer(app)
    await srv.start_server()
    yield srv
    await srv.close()


@pytest.fixture
async def ws(server: TestServer) -> AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
    session = aiohttp.ClientSession()
    conn = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    yield conn
    await conn.close()
    await session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _recv(ws: aiohttp.ClientWebSocketResponse, timeout: float = _RECV_TIMEOUT) -> Envelope:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    assert msg.type == aiohttp.WSMsgType.TEXT, f"Expected TEXT frame, got {msg.type}"
    return Envelope.from_json(str(msg.data))


async def _recv_response(
    ws: aiohttp.ClientWebSocketResponse, correlation_id: str, timeout: float = _RECV_TIMEOUT
) -> Envelope:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"No response for id={correlation_id!r} within {timeout}s")
        env = await _recv(ws, timeout=remaining)
        if env.kind == "response" and env.correlation_id == correlation_id:
            return env


def _make_request(msg_type: str, *, session_id: str | None = None, **payload: object) -> Envelope:
    body: dict[str, object] = {"type": msg_type, **payload}
    if session_id is not None:
        body["session_id"] = session_id
    return Envelope(kind="request", payload=body)


async def _hello(
    ws: aiohttp.ClientWebSocketResponse,
    *,
    session_id: str | None = None,
    window_id: str = "w1",
) -> Envelope:
    req = _make_request("hello", client="vsix", version="0.2.0", window_id=window_id)
    if session_id is not None:
        req.payload["session_id"] = session_id
    await ws.send_str(req.to_json())
    return await _recv_response(ws, req.id)


async def _open_session(ws: aiohttp.ClientWebSocketResponse) -> str:
    resp = await _hello(ws)
    return str(resp.payload["session_id"])


# ---------------------------------------------------------------------------
# hello — create / resume / ownership
# ---------------------------------------------------------------------------


async def test_hello_returns_version_and_session_id(ws: aiohttp.ClientWebSocketResponse) -> None:
    resp = await _hello(ws)
    assert resp.payload["type"] == "hello.ack"
    assert resp.payload["server_version"] == "0.2.0b1"
    assert resp.payload["session_id"]


async def test_control_hello_creates_no_session(server: TestServer) -> None:
    """A role=control connection (the sidebar) handshakes without a session."""
    session = aiohttp.ClientSession()
    c = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        req = _make_request("hello", client="vsix", window_id="wc", role="control")
        await c.send_str(req.to_json())
        resp = await _recv_response(c, req.id)
        assert resp.payload["type"] == "hello.ack"
        assert resp.payload["role"] == "control"
        assert "session_id" not in resp.payload  # no session was minted
        assert "cloud_registry" in resp.payload  # window-global llama/model snapshot
        assert "local_registry" in resp.payload
        # The control connection did not create any session.
        list_req = _make_request("session.list")
        await c.send_str(list_req.to_json())
        list_resp = await _recv_response(c, list_req.id)
        assert list_resp.payload["sessions"] == []
    finally:
        await c.close()
        await session.close()


async def test_hello_ack_embeds_state_snapshot(ws: aiohttp.ClientWebSocketResponse) -> None:
    resp = await _hello(ws)
    state = resp.payload["state"]
    assert isinstance(state, dict) and "phase" in state


async def test_hello_emits_state_event(ws: aiohttp.ClientWebSocketResponse) -> None:
    await _hello(ws)
    received: list[Envelope] = []
    for _ in range(5):
        try:
            received.append(await _recv(ws, timeout=2.0))
        except TimeoutError:
            break
        if any(e.kind == "event" and e.payload.get("type") == "state" for e in received):
            break
    assert any(e.kind == "event" and e.payload.get("type") == "state" for e in received)


async def test_two_windows_get_distinct_sessions(server: TestServer) -> None:
    session = aiohttp.ClientSession()
    a = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    b = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        sid_a = str((await _hello(a, window_id="wa")).payload["session_id"])
        sid_b = str((await _hello(b, window_id="wb")).payload["session_id"])
        assert sid_a != sid_b
    finally:
        await a.close()
        await b.close()
        await session.close()


async def test_resume_in_use_session_is_rejected(server: TestServer) -> None:
    session = aiohttp.ClientSession()
    a = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    b = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        sid = str((await _hello(a, window_id="wa")).payload["session_id"])
        resp = await _hello(b, session_id=sid, window_id="wb")
        assert resp.payload.get("error") == "session_in_use"
    finally:
        await a.close()
        await b.close()
        await session.close()


# ---------------------------------------------------------------------------
# unknown / session.list
# ---------------------------------------------------------------------------


async def test_unknown_message_returns_error(ws: aiohttp.ClientWebSocketResponse) -> None:
    req = _make_request("does.not.exist")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "unknown_message"


async def test_session_list_includes_open_session(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    req = _make_request("session.list")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    sessions = resp.payload["sessions"]
    assert isinstance(sessions, list)
    entry = next(s for s in sessions if s["id"] == sid)
    assert entry["taken"] is True
    assert entry["project_root"] is None  # problem-solving-only so far


# ---------------------------------------------------------------------------
# session-scoped handlers require a session_id
# ---------------------------------------------------------------------------


async def test_prompt_submit_with_text_returns_accepted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    req = _make_request("prompt.submit", session_id=sid, text="Build me a trading bot.")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "prompt.accepted"


async def test_prompt_submit_with_empty_text_returns_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    req = _make_request("prompt.submit", session_id=sid, text="")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "empty_prompt"


async def test_prompt_submit_unknown_session_errors(ws: aiohttp.ClientWebSocketResponse) -> None:
    req = _make_request("prompt.submit", session_id="nope", text="hi")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "unknown_session"


async def test_mode_set_autonomous_returns_accepted(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    req = _make_request("mode.set", session_id=sid, autonomous=True)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "mode.accepted"


async def test_stop_returns_accepted(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    req = _make_request("stop", session_id=sid)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "stop.accepted"


async def test_session_delete_closes_socket_and_drops_listing(server: TestServer) -> None:
    csession = aiohttp.ClientSession()
    conn = await csession.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    sid = ""
    try:
        sid = await _open_session(conn)
        req = _make_request("session.delete", session_id=sid)
        await conn.send_str(req.to_json())
        # The server closes the socket on success (possibly after a trailing
        # state event emitted by the engine stop). Drain until the close.
        closed = False
        for _ in range(10):
            msg = await asyncio.wait_for(conn.receive(), timeout=_RECV_TIMEOUT)
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                closed = True
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                env = Envelope.from_json(str(msg.data))
                assert env.payload.get("type") != "session.delete.error"
        assert closed
    finally:
        await conn.close()
        await csession.close()

    # A fresh connection no longer lists the deleted session. (session.list
    # needs no session of its own, so we don't open one — which would otherwise
    # reuse the just-freed timestamp id and reappear in the listing.)
    csession2 = aiohttp.ClientSession()
    conn2 = await csession2.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        req = _make_request("session.list")
        await conn2.send_str(req.to_json())
        resp = await _recv_response(conn2, req.id)
        ids = {s["id"] for s in resp.payload["sessions"]}
        assert sid not in ids
    finally:
        await conn2.close()
        await csession2.close()


async def test_session_delete_unknown_session_errors(server: TestServer) -> None:
    # autoping=False so the raw PONG frame below is observable via receive()
    # instead of being swallowed by aiohttp's automatic control-frame handling.
    csession = aiohttp.ClientSession()
    conn = await csession.ws_connect(f"http://127.0.0.1:{server.port}/ws", autoping=False)
    try:
        req = _make_request("session.delete", session_id="nope")
        await conn.send_str(req.to_json())
        resp = await _recv_response(conn, req.id)
        assert resp.payload["type"] == "error"
        assert resp.payload["code"] == "unknown_session"
        # The socket stays open: a raw WS ping still round-trips a pong.
        await conn.ping()
        msg = await asyncio.wait_for(conn.receive(), timeout=_RECV_TIMEOUT)
        assert msg.type == aiohttp.WSMsgType.PONG
    finally:
        await conn.close()
        await csession.close()


async def test_orphan_response_is_silently_dropped(server: TestServer) -> None:
    csession = aiohttp.ClientSession()
    conn = await csession.ws_connect(f"http://127.0.0.1:{server.port}/ws", autoping=False)
    try:
        orphan = Envelope(
            kind="response", correlation_id="no-such-request", payload={"action": "agree"}
        )
        await conn.send_str(orphan.to_json())
        # The socket stays open: a raw WS ping still round-trips a pong.
        await conn.ping()
        msg = await asyncio.wait_for(conn.receive(), timeout=_RECV_TIMEOUT)
        assert msg.type == aiohttp.WSMsgType.PONG
    finally:
        await conn.close()
        await csession.close()


# ---------------------------------------------------------------------------
# checkpoint.* — full wire protocol against a real RootMirrorManager-backed
# root (no LLM/tool-dispatch involved: the checkpoint history is seeded
# directly via RootMirrorManager, the same on-disk artifacts a real tool
# call would produce — see test_checkpoint_state.py for the engine-level
# coverage this builds on).
# ---------------------------------------------------------------------------


async def _recv_until_response(
    ws: aiohttp.ClientWebSocketResponse, correlation_id: str, timeout: float = _RECV_TIMEOUT
) -> tuple[Envelope, list[Envelope]]:
    """Like _recv_response, but also returns every event seen along the way."""
    events: list[Envelope] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"No response for id={correlation_id!r} within {timeout}s")
        env = await _recv(ws, timeout=remaining)
        if env.kind == "response" and env.correlation_id == correlation_id:
            return env, events
        if env.kind == "event":
            events.append(env)


async def _seed_two_checkpoints(root: Path) -> tuple[str, str]:
    """Create a real two-checkpoint mirror history at *root* and return the shas."""
    from kodo.runtime._checkpoints import RootMirrorManager

    mgr = RootMirrorManager([root])
    await mgr.prepare(root / "a.txt")
    (root / "a.txt").write_text("one\n")
    ref1 = await mgr.commit_for_path(root / "a.txt", "create a")
    assert ref1 is not None
    await mgr.prepare(root / "a.txt")
    (root / "a.txt").write_text("two\n")
    ref2 = await mgr.commit_for_path(root / "a.txt", "edit a")
    assert ref2 is not None
    return ref1.sha, ref2.sha


async def test_checkpoint_list_returns_seeded_state(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    req = _make_request("checkpoint.list", session_id=sid, root=str(root))
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "checkpoint.list.done"
    assert resp.payload["current_index"] == 1
    entries = resp.payload["entries"]
    assert [e["sha"] for e in entries] == [sha1, sha2]
    assert all(e["undone"] is False for e in entries)


async def test_checkpoint_undo_flips_undone_and_pushes_state(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, _sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    req = _make_request("checkpoint.undo", session_id=sid, root=str(root), sha=sha1)
    await ws.send_str(req.to_json())
    resp, events = await _recv_until_response(ws, req.id)

    assert resp.payload["type"] == "checkpoint.undo.done"
    # entries grew by one (the undo itself is a new forward commit).
    entries = resp.payload["entries"]
    assert len(entries) == 3
    assert entries[0]["sha"] == sha1
    assert entries[0]["undone"] is True
    assert resp.payload["current_index"] == 2
    assert not (root / "a.txt").exists()

    state_events = [e for e in events if e.payload.get("type") == "checkpoint.state"]
    assert len(state_events) == 1
    assert state_events[0].payload["root"] == str(root)
    assert state_events[0].payload["current_index"] == 2


async def test_checkpoint_rollback_then_roll_forward(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    req = _make_request("checkpoint.rollback", session_id=sid, root=str(root), sha=sha1)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.rollback.done"
    assert resp.payload["current_index"] == 0
    assert (root / "a.txt").read_text() == "one\n"

    req = _make_request("checkpoint.roll_forward", session_id=sid, root=str(root), sha=sha2)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.roll_forward.done"
    assert resp.payload["current_index"] == 1
    assert (root / "a.txt").read_text() == "two\n"


async def test_checkpoint_undo_on_dirty_tree_needs_confirmation_then_stash(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, _sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    # An edit made outside of Kodo, never committed to the mirror.
    (root / "untracked.txt").write_text("surprise\n")

    req = _make_request("checkpoint.undo", session_id=sid, root=str(root), sha=sha1)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.undo.needs_confirmation"
    assert resp.payload["root"] == str(root)
    assert resp.payload["sha"] == sha1
    # Nothing was touched — the dirty file is still there untouched.
    assert (root / "untracked.txt").read_text() == "surprise\n"
    assert (root / "a.txt").read_text() == "two\n"

    req = _make_request(
        "checkpoint.undo", session_id=sid, root=str(root), sha=sha1, resolution="stash"
    )
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.undo.done"
    assert not (root / "a.txt").exists()
    # Stashed change reapplied afterwards.
    assert (root / "untracked.txt").read_text() == "surprise\n"


# ---------------------------------------------------------------------------
# local_llm.install — the fire-and-forget background download pushes a
# second local_llm.registry_state once it actually finishes, not just the
# immediate kickoff one (see _run_background_download in _app.py).
# ---------------------------------------------------------------------------


def _local_entry(payload: dict[str, object], name: str) -> dict[str, object]:
    registry = cast("list[dict[str, object]]", payload["local_registry"])
    return next(e for e in registry if e["name"] == name)


async def test_local_llm_install_pushes_registry_state_again_on_completion(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.llms.local import LocalModelManager

    req = _make_request(
        "local_llm.add_huggingface",
        name="test-model",
        description="",
        repo_id="acme/test-model",
        filename="model.gguf",
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    assert added.payload["type"] == "local_llm.registry_state"
    assert _local_entry(added.payload, "test-model")["installed"] is False

    # download_model would otherwise block on a real HF fetch; get_model_path
    # is what _local_registry_payload consults for installed/installed_path,
    # so faking both — gated on the same "has the fake download run yet" flag
    # — reproduces the real installed=False-then-True transition without
    # touching the real transfer machinery (covered separately by
    # test_llms_local.py).
    downloaded = {"done": False}

    async def _fake_download(self: object, *a: object, **k: object) -> None:
        downloaded["done"] = True

    monkeypatch.setattr(LocalModelManager, "download_model", _fake_download)
    monkeypatch.setattr(
        LocalModelManager,
        "get_model_path",
        lambda self, name: Path("/fake/model.gguf") if downloaded["done"] else None,
    )

    req = _make_request("local_llm.install", name="test-model")
    await ws.send_str(req.to_json())

    kickoff = await _recv(ws)
    assert kickoff.payload["type"] == "local_llm.registry_state"
    assert _local_entry(kickoff.payload, "test-model")["installed"] is False

    completed = await _recv(ws)
    assert completed.payload["type"] == "local_llm.registry_state"
    completed_entry = _local_entry(completed.payload, "test-model")
    assert completed_entry["installed"] is True
    assert completed_entry["installed_path"] == "/fake/model.gguf"


async def test_local_llm_install_pushes_registry_state_after_failure_too(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.llms.local import LocalModelError, LocalModelManager

    req = _make_request(
        "local_llm.add_huggingface",
        name="test-model",
        description="",
        repo_id="acme/test-model",
        filename="model.gguf",
    )
    await ws.send_str(req.to_json())
    await _recv(ws)  # kickoff-of-add registry_state, not under test here

    async def _boom(self: object, *a: object, **k: object) -> None:
        raise LocalModelError("network is on fire")

    monkeypatch.setattr(LocalModelManager, "download_model", _boom)

    req = _make_request("local_llm.install", name="test-model")
    await ws.send_str(req.to_json())

    await _recv(ws)  # kickoff registry_state

    error_evt = await _recv(ws)
    assert error_evt.payload["type"] == "error"
    assert error_evt.payload["code"] == "local_llm_error"
    assert "network is on fire" in error_evt.payload["message"]

    completed = await _recv(ws)
    assert completed.payload["type"] == "local_llm.registry_state"
    assert _local_entry(completed.payload, "test-model")["installed"] is False

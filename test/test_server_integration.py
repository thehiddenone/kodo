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

    # _start_background fire-and-forgets kodo.titling.start_titling on
    # startup when llama.cpp is installed (doc/INTERNALS.md §10c); stubbed
    # here so server boot stays fully offline/deterministic like the rest of
    # this file (see module docstring) instead of racing a real model
    # download/llama-server spin-up every test.
    async def _no_op_start_titling(kodo_dir: Path) -> None:
        return None

    monkeypatch.setattr(_app_module, "start_titling", _no_op_start_titling)

    # SessionTitler awaits kodo.titling.generate_title fire-and-forget on the
    # first prompt of every session (runtime/_engine/_titling.py) — stubbed
    # here too, otherwise any test that submits a real prompt triggers a real
    # HTTP call against a titler llama-server that was never started.
    async def _no_op_generate_title(text: str) -> None:
        return None

    monkeypatch.setattr(_titling_module, "generate_title", _no_op_generate_title)
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
    assert Path(completed_entry["installed_path"]) == Path("/fake/model.gguf")


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


async def test_add_huggingface_seeds_a_default_flavor_from_its_llama_args(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """Flavors are the only source of launch args — see doc/LLM_REGISTRY.md
    §4.6 — so a freshly-added custom_hf entry's own `llama_args` field (still
    collected by the "Add local LLM" modal) must end up seeding its first
    (custom) flavor rather than being dropped on the floor."""
    req = _make_request(
        "local_llm.add_huggingface",
        name="test-model",
        description="",
        repo_id="acme/test-model",
        filename="model.gguf",
        llama_args={"--cache-type-k": "q8_0"},
        context_window=32768,
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    entry = _local_entry(added.payload, "test-model")
    assert entry["active_flavor"] == ""
    flavors = cast("list[dict[str, object]]", entry["flavors"])
    assert len(flavors) == 1
    assert flavors[0]["id"] == "default"
    assert flavors[0]["llama_args"] == {"--cache-type-k": "q8_0"}
    assert flavors[0]["predefined"] is False


# ---------------------------------------------------------------------------
# Flavors (local_llm.add_flavor / .update_flavor / .remove_flavor /
# .set_active_flavor) —
# see doc/LLM_REGISTRY.md §4.6. Uses a real hardcoded entry name (no download
# needed — flavors don't touch the download manager) so add_flavor's own
# entry-existence check passes without first adding a custom entry. Every
# hardcoded entry ships a built-in "default" flavor (predefined=True), so
# `flavors` always has at least that one even before any custom flavor is
# added — the tests below account for it rather than assuming an empty list.
# ---------------------------------------------------------------------------

_FLAVOR_TEST_ENTRY = "unsloth-qwen35-9b-q8-k-xl"


def _flavor_ids(entry: dict[str, object]) -> list[str]:
    return [cast("dict[str, object]", f)["id"] for f in cast("list[object]", entry["flavors"])]


def _custom_flavor_ids(entry: dict[str, object]) -> list[str]:
    return [
        cast("dict[str, object]", f)["id"]
        for f in cast("list[object]", entry["flavors"])
        if not cast("dict[str, object]", f)["predefined"]
    ]


async def test_add_flavor_appears_in_registry_state(ws: aiohttp.ClientWebSocketResponse) -> None:
    req = _make_request(
        "local_llm.add_flavor",
        name=_FLAVOR_TEST_ENTRY,
        flavor_name="1M Context",
        description="YaRN 1M",
        llama_args_text="--ctx-size 1048576\n--rope-scaling yarn",
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    assert resp.payload["type"] == "local_llm.registry_state"
    entry = _local_entry(resp.payload, _FLAVOR_TEST_ENTRY)
    assert entry["active_flavor"] == ""
    flavors = cast("list[dict[str, object]]", entry["flavors"])
    assert len(flavors) == 2
    assert flavors[0]["id"] == "default"
    assert flavors[0]["predefined"] is True
    added_flavor = flavors[1]
    assert added_flavor["id"] == "1m-context"
    assert added_flavor["name"] == "1M Context"
    assert added_flavor["llama_args"] == {"--ctx-size": "1048576", "--rope-scaling": "yarn"}
    assert added_flavor["predefined"] is False
    # min_ram/min_vram weren't in this request — a brand-new custom flavor
    # defaults to the hardware-fit check being inactive (see LlamaFlavor's
    # docstring, doc/LLM_REGISTRY.md §4.6a).
    assert added_flavor["min_ram"] == 0
    assert added_flavor["min_vram"] == 0


async def test_add_flavor_can_set_min_ram_and_min_vram(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_flavor",
        name=_FLAVOR_TEST_ENTRY,
        flavor_name="Mac Flavor",
        min_ram=64,
        min_vram=0,
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    entry = _local_entry(resp.payload, _FLAVOR_TEST_ENTRY)
    added_flavor = next(
        cast("dict[str, object]", f)
        for f in cast("list[object]", entry["flavors"])
        if cast("dict[str, object]", f)["id"] == "mac-flavor"
    )
    assert added_flavor["min_ram"] == 64
    assert added_flavor["min_vram"] == 0


async def test_update_flavor_on_a_custom_flavor_edits_it_in_place(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_flavor",
        name=_FLAVOR_TEST_ENTRY,
        flavor_name="Tight VRAM",
        min_ram=16,
        min_vram=8,
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    flavor_id = _custom_flavor_ids(_local_entry(added.payload, _FLAVOR_TEST_ENTRY))[0]

    req = _make_request(
        "local_llm.update_flavor",
        name=_FLAVOR_TEST_ENTRY,
        flavor_id=flavor_id,
        flavor_name="Tight VRAM (v2)",
        description="updated",
        llama_args_text="--n-cpu-moe 20",
        min_ram=24,
        min_vram=12,
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    entry = _local_entry(resp.payload, _FLAVOR_TEST_ENTRY)
    flavors = cast("list[dict[str, object]]", entry["flavors"])
    updated = next(f for f in flavors if f["id"] == flavor_id)
    assert updated["name"] == "Tight VRAM (v2)"
    assert updated["description"] == "updated"
    assert updated["llama_args"] == {"--n-cpu-moe": "20"}
    assert updated["predefined"] is False
    assert updated["min_ram"] == 24
    assert updated["min_vram"] == 12


async def test_update_flavor_rejects_a_predefined_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    # Predefined flavors are strictly read-only — editing "default" (or any
    # other predefined id) is rejected outright, no override created.
    req = _make_request(
        "local_llm.update_flavor",
        name=_FLAVOR_TEST_ENTRY,
        flavor_id="default",
        flavor_name="Default (edited)",
        llama_args_text="--cache-type-k q8_0\n--cache-type-v q8_0\n--ctx-size 0\n--jinja",
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_update_flavor_rejects_unknown_flavor_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.update_flavor",
        name=_FLAVOR_TEST_ENTRY,
        flavor_id="nonexistent",
        flavor_name="Whatever",
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_add_flavor_rejects_custom_server_url_entry(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_server_url", name="remote", description="", url="http://host:8042"
    )
    await ws.send_str(req.to_json())
    await _recv(ws)  # add's own registry_state, not under test here

    req = _make_request("local_llm.add_flavor", name="remote", flavor_name="whatever")
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_add_flavor_dedupes_id_when_different_names_share_a_slug(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    resp = None
    for flavor_name in ("Tight VRAM", "tight vram"):
        req = _make_request(
            "local_llm.add_flavor", name=_FLAVOR_TEST_ENTRY, flavor_name=flavor_name
        )
        await ws.send_str(req.to_json())
        resp = await _recv(ws)
    assert resp is not None
    ids = sorted(_flavor_ids(_local_entry(resp.payload, _FLAVOR_TEST_ENTRY)))
    assert ids == ["default", "tight-vram", "tight-vram-2"]


async def test_add_flavor_rejects_duplicate_name(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request("local_llm.add_flavor", name=_FLAVOR_TEST_ENTRY, flavor_name="Tight VRAM")
    await ws.send_str(req.to_json())
    await _recv(ws)
    req = _make_request("local_llm.add_flavor", name=_FLAVOR_TEST_ENTRY, flavor_name="Tight VRAM")
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"
    assert "already exists" in str(err.payload["message"])


async def test_set_active_flavor_then_remove_resets_to_default(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request("local_llm.add_flavor", name=_FLAVOR_TEST_ENTRY, flavor_name="Tight VRAM")
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    flavor_id = _custom_flavor_ids(_local_entry(added.payload, _FLAVOR_TEST_ENTRY))[0]

    req = _make_request("local_llm.set_active_flavor", name=_FLAVOR_TEST_ENTRY, flavor_id=flavor_id)
    await ws.send_str(req.to_json())
    active = await _recv(ws)
    assert active.payload["type"] == "local_llm.registry_state"
    assert _local_entry(active.payload, _FLAVOR_TEST_ENTRY)["active_flavor"] == flavor_id

    req = _make_request("local_llm.remove_flavor", name=_FLAVOR_TEST_ENTRY, flavor_id=flavor_id)
    await ws.send_str(req.to_json())
    removed = await _recv(ws)
    entry = _local_entry(removed.payload, _FLAVOR_TEST_ENTRY)
    # Removing the only custom flavor leaves just the built-in default.
    flavors = cast("list[dict[str, object]]", entry["flavors"])
    assert len(flavors) == 1
    assert flavors[0]["id"] == "default"
    assert flavors[0]["predefined"] is True
    assert entry["active_flavor"] == ""


async def test_set_active_flavor_rejects_unknown_flavor_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.set_active_flavor", name=_FLAVOR_TEST_ENTRY, flavor_id="nonexistent"
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_remove_flavor_rejects_unknown_flavor_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request("local_llm.remove_flavor", name=_FLAVOR_TEST_ENTRY, flavor_id="nonexistent")
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_set_active_flavor_for_currently_selected_model_does_not_crash_without_server(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """Exercises the restart-check path (_restart_llama_server_if_running) for
    the entry that IS the currently selected local model — it must no-op
    cleanly when nothing is actually running (llama.cpp isn't installed in
    this sandboxed test environment), not raise. The actual subprocess
    restart itself is out of scope here, same as llm.select's (untested
    elsewhere in this file for the same reason)."""
    # Persist models.local = _FLAVOR_TEST_ENTRY the same way llm.select does,
    # without requiring a real llama-server process to actually start.
    req = _make_request("llm.select", name=_FLAVOR_TEST_ENTRY)
    await ws.send_str(req.to_json())
    await _recv(ws)  # llama.state {running: false, error: "llama.cpp is not installed"}
    select_done = await _recv_response(ws, req.id)
    assert select_done.payload["ok"] is False

    req = _make_request("local_llm.add_flavor", name=_FLAVOR_TEST_ENTRY, flavor_name="Tight VRAM")
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    flavor_id = _custom_flavor_ids(_local_entry(added.payload, _FLAVOR_TEST_ENTRY))[0]

    req = _make_request("local_llm.set_active_flavor", name=_FLAVOR_TEST_ENTRY, flavor_id=flavor_id)
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    assert resp.payload["type"] == "local_llm.registry_state"
    assert _local_entry(resp.payload, _FLAVOR_TEST_ENTRY)["active_flavor"] == flavor_id

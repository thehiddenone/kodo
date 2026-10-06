"""Session-scoped handlers of the WebSocket server app, driven over a real socket.

Every handler here resolves ``payload.session_id`` to a live session first and
answers ``unknown_session`` when it can't; the rest of each test checks the
handler's own reply shape. Engine operations that would need a real project,
git mirror or model (project creation, checkpoint moves) are replaced on
:class:`~kodo.runtime.WorkflowEngine` so the wire contract is what is under
test, not the engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.llms.llamacpp import LlamaServer
from kodo.project import ProjectLayoutError
from kodo.runtime import CheckpointEntry, CheckpointState, MirrorDirtyError, WorkflowEngine
from kodo.server import Config, create_app

_RECV_TIMEOUT = 5.0
_APP = "kodo.server._app"


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    async def _no_op_start_titling(*_a: object, **_k: object) -> None:
        return None

    async def _no_op() -> None:
        return None

    async def _no_op_refresh_loop(_kodo_dir: Path) -> None:
        return None

    monkeypatch.setattr(f"{_APP}.start_titling", _no_op_start_titling)
    monkeypatch.setattr(f"{_APP}.stop_titling", _no_op)
    monkeypatch.setattr(f"{_APP}.run_openrouter_catalog_refresh_loop", _no_op_refresh_loop)
    monkeypatch.setattr(f"{_APP}.ensure_all_utils", lambda _kodo_dir: {})
    monkeypatch.setattr(f"{_APP}.find_running_server", lambda _kodo_dir: None)
    monkeypatch.setattr(f"{_APP}.find_installed", lambda _kodo_dir: None)
    monkeypatch.setattr(LlamaServer, "get_active_llama_server", classmethod(lambda cls: None))
    return home


@pytest.fixture
async def ws() -> AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
    srv = TestServer(create_app(Config()))
    await srv.start_server()
    session = aiohttp.ClientSession()
    conn = await session.ws_connect(f"http://127.0.0.1:{srv.port}/ws")
    yield conn
    await conn.close()
    await session.close()
    await srv.close()


async def _request(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str, **payload: object
) -> dict[str, object]:
    """Send a request and return the payload of its correlated response."""
    req = Envelope(kind="request", payload={"type": msg_type, **payload})
    await ws.send_str(req.to_json())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RECV_TIMEOUT
    while True:
        remaining = max(0.01, deadline - loop.time())
        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        assert msg.type == aiohttp.WSMsgType.TEXT, f"Expected TEXT frame, got {msg.type}"
        env = Envelope.from_json(str(msg.data))
        if env.kind == "response" and env.correlation_id == req.id:
            return env.payload


async def _open_session(ws: aiohttp.ClientWebSocketResponse) -> str:
    ack = await _request(ws, "hello", client="test", window_id="w1")
    return str(ack["session_id"])


def _state(*shas: str) -> CheckpointState:
    entries = [
        CheckpointEntry(sha=sha, parent="", label=f"edit {sha}", kind="tool_call") for sha in shas
    ]
    return CheckpointState(entries=entries, current_index=len(entries) - 1)


# ---------------------------------------------------------------------------
# Every session-scoped handler rejects an unknown session the same way
# ---------------------------------------------------------------------------

_SESSION_SCOPED = (
    "mode.set",
    "agent.set",
    "edit_control.set",
    "command_control.set",
    "thinking_level.set",
    "sampling.set",
    "workspace.folders",
    "project.create",
    "stop",
    "compact.now",
    "checkpoint.rollback",
    "checkpoint.roll_forward",
    "checkpoint.undo",
    "checkpoint.redo",
    "checkpoint.list",
)


@pytest.mark.parametrize("msg_type", _SESSION_SCOPED)
async def test_session_scoped_handler_rejects_an_unknown_session(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str
) -> None:
    reply = await _request(ws, msg_type, session_id="no-such-session")
    assert reply["type"] == "error"
    assert reply["code"] == "unknown_session"
    assert reply["recoverable"] is True


# ---------------------------------------------------------------------------
# Per-session settings
# ---------------------------------------------------------------------------


async def test_edit_control_set_is_accepted_and_reflected_in_state(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _request(ws, "edit_control.set", session_id=sid, edit_control="review_all")
    assert reply == {"type": "edit_control.accepted"}
    resumed = await _request(ws, "hello", client="test", window_id="w1", session_id=sid)
    assert resumed["state"]["edit_control"] == "review_all"  # type: ignore[index]


async def test_command_control_set_is_accepted_and_reflected_in_state(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _request(ws, "command_control.set", session_id=sid, command_control="defensive")
    assert reply == {"type": "command_control.accepted"}
    resumed = await _request(ws, "hello", client="test", window_id="w1", session_id=sid)
    assert resumed["state"]["command_control"] == "defensive"  # type: ignore[index]


async def test_thinking_level_set_reports_whether_it_was_applied(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _request(ws, "thinking_level.set", session_id=sid, thinking_level="no-such-tier")
    assert reply == {"type": "thinking_level.accepted", "ok": False}


async def test_sampling_set_for_an_unknown_model_is_not_ok(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _request(
        ws, "sampling.set", session_id=sid, model="no-such-model", sampling={"temperature": 0.3}
    )
    assert reply["type"] == "sampling.accepted"
    assert reply["ok"] is False


async def test_sampling_set_with_a_non_dict_sampling_is_tolerated(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _request(ws, "sampling.set", session_id=sid, model="", sampling="hot")
    assert reply["type"] == "sampling.accepted"
    assert reply["ok"] is False


async def test_workspace_folders_is_acked(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    sid = await _open_session(ws)
    reply = await _request(
        ws,
        "workspace.folders",
        session_id=sid,
        physical_root=str(root),
        folders={"ws": str(root)},
        code_workspace_file=str(tmp_path / "x.code-workspace"),
    )
    assert reply == {"type": "workspace.folders.ack"}


async def test_workspace_folders_tolerates_malformed_fields(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _request(
        ws, "workspace.folders", session_id=sid, folders="nope", code_workspace_file=""
    )
    assert reply == {"type": "workspace.folders.ack"}


async def test_compact_now_is_accepted(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    reply = await _request(ws, "compact.now", session_id=sid)
    assert reply == {"type": "compact.accepted"}


# ---------------------------------------------------------------------------
# project.create
# ---------------------------------------------------------------------------


async def test_project_create_requires_a_path_or_name(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    reply = await _request(ws, "project.create", session_id=sid)
    assert reply["type"] == "error"
    assert reply["code"] == "missing_project_name_or_path"


async def test_project_create_replies_done_with_the_engine_result(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str | None, bool]] = []

    async def _create(
        self: WorkflowEngine, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        calls.append((name, path, force))
        return {"path": path or name, "name": name}

    monkeypatch.setattr(WorkflowEngine, "handle_project_create", _create)
    sid = await _open_session(ws)
    target = str(tmp_path / "proj")
    reply = await _request(
        ws, "project.create", session_id=sid, path=target, name="Proj", force=True
    )
    assert reply == {"type": "project.create.done", "path": target, "name": "Proj"}
    assert calls == [("Proj", target, True)]


async def test_project_create_layout_error_is_reported(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _create(
        self: WorkflowEngine, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        raise ProjectLayoutError("kodo.md already exists")

    monkeypatch.setattr(WorkflowEngine, "handle_project_create", _create)
    sid = await _open_session(ws)
    reply = await _request(ws, "project.create", session_id=sid, name="Proj")
    assert reply == {"type": "project.create.error", "message": "kodo.md already exists"}


# ---------------------------------------------------------------------------
# checkpoint.* — dirty-tree confirmation and redo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msg_type", "verb", "method"),
    [
        ("checkpoint.rollback", "rollback", "handle_checkpoint_rollback"),
        ("checkpoint.roll_forward", "roll_forward", "handle_checkpoint_roll_forward"),
        ("checkpoint.redo", "redo", "handle_checkpoint_redo"),
    ],
)
async def test_checkpoint_op_on_a_dirty_tree_needs_confirmation(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    msg_type: str,
    verb: str,
    method: str,
) -> None:
    async def _dirty(
        self: WorkflowEngine, root: str, sha: str, resolution: str | None
    ) -> CheckpointState:
        raise MirrorDirtyError("untracked edits")

    monkeypatch.setattr(WorkflowEngine, method, _dirty)
    sid = await _open_session(ws)
    reply = await _request(ws, msg_type, session_id=sid, root="/r", sha="abc")
    assert reply == {"type": f"checkpoint.{verb}.needs_confirmation", "root": "/r", "sha": "abc"}


async def test_checkpoint_redo_replies_with_the_new_state_and_passes_the_resolution(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolutions: list[str | None] = []

    async def _redo(
        self: WorkflowEngine, root: str, sha: str, resolution: str | None
    ) -> CheckpointState:
        resolutions.append(resolution)
        return _state("aaa", "bbb")

    monkeypatch.setattr(WorkflowEngine, "handle_checkpoint_redo", _redo)
    sid = await _open_session(ws)
    reply = await _request(
        ws, "checkpoint.redo", session_id=sid, root="/r", sha="aaa", resolution="discard"
    )
    assert reply == {
        "type": "checkpoint.redo.done",
        "root": "/r",
        "sha": "aaa",
        "current_index": 1,
        "entries": [{"sha": "aaa", "undone": False}, {"sha": "bbb", "undone": False}],
    }
    assert resolutions == ["discard"]


async def test_checkpoint_resolution_that_is_not_a_string_is_ignored(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolutions: list[str | None] = []

    async def _undo(
        self: WorkflowEngine, root: str, sha: str, resolution: str | None
    ) -> CheckpointState:
        resolutions.append(resolution)
        return _state("aaa")

    monkeypatch.setattr(WorkflowEngine, "handle_checkpoint_undo", _undo)
    sid = await _open_session(ws)
    reply = await _request(
        ws, "checkpoint.undo", session_id=sid, root="/r", sha="aaa", resolution=7
    )
    assert reply["type"] == "checkpoint.undo.done"
    assert resolutions == [None]

"""Tests for the automated validation harness (``kodo.validator``).

Covers the non-LLM capabilities: isolated home cloning, workspace simulation,
the scripted user, and the protocol client — the latter both against the real
in-process server app (hello / workspace push / mode commands) and against a
stub WebSocket server that drives server→client requests and phase events
(simulated interactions, turn-end detection). No LLM calls are made and no
subprocess is spawned.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.runtime._engine import _titling as _titling_module
from kodo.server import Config, create_app
from kodo.server import _app as _app_module
from kodo.validator import (
    LocalModelUnavailableError,
    ProtocolError,
    QuestionAnswer,
    ScriptedUser,
    SimulatedWorkspace,
    Transcript,
    ValidatorClient,
    clone_kodo_home,
    ensure_local_llms_installed,
)
from kodo.validator import _models as validator_models

_RECV_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "test-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # See test_server_integration.py's _temp_home: keep server boot fully
    # offline instead of racing a real HuggingFace download every test.
    monkeypatch.setattr(_app_module, "warm_up_titler_cache", lambda: None)
    monkeypatch.setattr(_titling_module, "generate_title", lambda text: None)
    return home


# ---------------------------------------------------------------------------
# clone_kodo_home
# ---------------------------------------------------------------------------


def _make_template(base: Path) -> Path:
    template = base / "template-kodo"
    (template / "bin").mkdir(parents=True)
    (template / "bin" / "tool").write_text("binary")
    (template / "llama.cpp" / "models").mkdir(parents=True)
    (template / "llama.cpp" / "models" / "m.gguf").write_text("weights")
    (template / "sessions" / "123").mkdir(parents=True)
    (template / "logs").mkdir()
    (template / "logs" / "server.log").write_text("old logs")
    (template / "kodo-server").write_text('{"pid": 1, "port": 9042}')
    (template / "etc").mkdir()
    (template / "etc" / "settings.json").write_text(
        json.dumps({"mode": "local", "models": {"local": "some-model"}})
    )
    return template


def test_clone_home_symlinks_copies_and_skips(tmp_path: Path) -> None:
    template = _make_template(tmp_path)
    home = tmp_path / "run-home"

    kodo_dir = clone_kodo_home(home, template)

    assert kodo_dir == home / ".kodo"
    # bin/ and llama.cpp/ are symlinks into the template.
    assert (kodo_dir / "bin").is_symlink()
    assert (kodo_dir / "llama.cpp").is_symlink()
    assert (kodo_dir / "llama.cpp" / "models" / "m.gguf").read_text() == "weights"
    # Per-run state starts fresh.
    assert not (kodo_dir / "kodo-server").exists()
    assert list((kodo_dir / "sessions").iterdir()) == []
    assert list((kodo_dir / "logs").iterdir()) == []
    # etc/ is a real copy, not a link.
    assert not (kodo_dir / "etc").is_symlink()
    settings = json.loads((kodo_dir / "etc" / "settings.json").read_text())
    assert settings["mode"] == "local"


def test_clone_home_applies_settings_overrides(tmp_path: Path) -> None:
    template = _make_template(tmp_path)
    home = tmp_path / "run-home"

    kodo_dir = clone_kodo_home(
        home, template, settings_overrides={"mode": "cloud", "models": {"extra": "x"}}
    )

    settings = json.loads((kodo_dir / "etc" / "settings.json").read_text())
    assert settings["mode"] == "cloud"
    # Deep merge: the template's nested key survives alongside the override.
    assert settings["models"] == {"local": "some-model", "extra": "x"}


def test_clone_home_without_template_creates_skeleton(tmp_path: Path) -> None:
    kodo_dir = clone_kodo_home(tmp_path / "run-home")
    assert (kodo_dir / "sessions").is_dir()
    assert (kodo_dir / "logs").is_dir()
    assert (kodo_dir / "etc").is_dir()


def test_clone_home_missing_template_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        clone_kodo_home(tmp_path / "run-home", tmp_path / "nope")


# ---------------------------------------------------------------------------
# SimulatedWorkspace
# ---------------------------------------------------------------------------


def test_workspace_single_and_multi_root_payload(tmp_path: Path) -> None:
    ws = SimulatedWorkspace(tmp_path / "ws")
    ws.add_root("app")
    payload = ws.folders_payload()
    assert payload["physical_root"] == str((tmp_path / "ws").resolve())
    assert payload["folders"] == {"app": str((tmp_path / "ws" / "app").resolve())}

    ws.add_root("lib")
    folders = cast(dict[str, str], ws.folders_payload()["folders"])
    assert set(folders) == {"app", "lib"}


def test_workspace_seeding_and_write(tmp_path: Path) -> None:
    source = tmp_path / "seed"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "a.py").write_text("A = 1\n")

    ws = SimulatedWorkspace(tmp_path / "ws")
    ws.add_root("app", seed_from=source)
    assert (ws.root_path("app") / "pkg" / "a.py").read_text() == "A = 1\n"

    single = tmp_path / "extra.txt"
    single.write_text("extra")
    ws.seed("app", single, dest_rel="docs/extra.txt")
    assert (ws.root_path("app") / "docs" / "extra.txt").read_text() == "extra"

    ws.write_file("app", "src/new.py", "B = 2\n")
    assert (ws.root_path("app") / "src" / "new.py").read_text() == "B = 2\n"


def test_workspace_duplicate_root_rejected(tmp_path: Path) -> None:
    ws = SimulatedWorkspace(tmp_path / "ws")
    ws.add_root("app")
    with pytest.raises(ValueError, match="already exists"):
        ws.add_root("app")


# ---------------------------------------------------------------------------
# ScriptedUser
# ---------------------------------------------------------------------------


async def test_scripted_user_default_answers() -> None:
    user = ScriptedUser(free_text_fallback="go on")
    response = await user.answer_questions(
        {
            "questions": [
                {"question": "DB?", "kind": "single_choice", "options": ["PostgreSQL", "SQLite"]},
                {"question": "Anything else?", "kind": "single_choice", "options": []},
            ]
        }
    )
    assert response["type"] == "prompt.question.response"
    answers = cast(list[dict[str, object]], response["answers"])
    assert answers[0] == {"selected": ["PostgreSQL"], "free_text": None}
    assert answers[1] == {"selected": [], "free_text": "go on"}


async def test_scripted_user_consumes_script_and_pads() -> None:
    user = ScriptedUser(
        question_script=[[QuestionAnswer(selected=["SQLite"])]],
        free_text_fallback="default",
    )
    response = await user.answer_questions(
        {
            "questions": [
                {"question": "DB?", "options": ["PostgreSQL", "SQLite"]},
                {"question": "Scope?", "options": ["Small", "Large"]},
            ]
        }
    )
    answers = cast(list[dict[str, object]], response["answers"])
    assert answers[0]["selected"] == ["SQLite"]
    # Script batch was short by one: padded with the default (first option).
    assert answers[1]["selected"] == ["Small"]

    # Script exhausted: back to defaults.
    response = await user.answer_questions({"questions": [{"question": "?", "options": ["A"]}]})
    assert cast(list[dict[str, object]], response["answers"])[0]["selected"] == ["A"]


async def test_scripted_user_gate_answers() -> None:
    user = ScriptedUser(permission_action="deny", permission_feedback="not in tests")
    permission = await user.answer_permission({"tool_name": "run_command"})
    assert permission == {
        "type": "prompt.permission.response",
        "action": "deny",
        "feedback": "not in tests",
    }
    approval = await user.answer_approval({"gate_type": "document_review"})
    assert approval["action"] == "agree"
    assert approval["feedback_text"] is None


async def test_scripted_user_api_key_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = ScriptedUser(api_keys={"anthropic": "sk-explicit"})
    assert await explicit.provide_api_key("anthropic") == "sk-explicit"

    env_user = ScriptedUser()
    monkeypatch.setenv("KODO_VALIDATOR_API_KEY_ANTHROPIC", "sk-validator")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-generic")
    assert await env_user.provide_api_key("anthropic") == "sk-validator"

    monkeypatch.delenv("KODO_VALIDATOR_API_KEY_ANTHROPIC")
    assert await env_user.provide_api_key("anthropic") == "sk-generic"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert await env_user.provide_api_key("anthropic") is None


# ---------------------------------------------------------------------------
# ValidatorClient against the real in-process server app
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_server() -> AsyncGenerator[TestServer, None]:
    app = create_app(Config())
    srv = TestServer(app)
    await srv.start_server()
    yield srv
    await srv.close()


@pytest.fixture
async def client(real_server: TestServer) -> AsyncGenerator[ValidatorClient, None]:
    c = ValidatorClient(f"ws://127.0.0.1:{real_server.port}/ws", Transcript(), ScriptedUser())
    await c.connect()
    yield c
    await c.close()


async def test_client_hello_binds_session(client: ValidatorClient) -> None:
    ack = await client.hello()
    assert ack["type"] == "hello.ack"
    assert client.session_id
    assert client.phase is not None


async def test_client_pushes_workspace_and_modes(client: ValidatorClient, tmp_path: Path) -> None:
    await client.hello()
    ws = SimulatedWorkspace(tmp_path / "ws")
    ws.add_root("app")
    ws.add_root("lib")

    ack = await client.request("workspace.folders", **ws.folders_payload())
    assert ack["type"] == "workspace.folders.ack"

    assert (await client.request("mode.set", autonomous=True))["type"] == "mode.accepted"
    assert (await client.request("workflow.set", mode="problem_solving"))[
        "type"
    ] == "workflow.accepted"
    assert (await client.request("edit_control.set", edit_control="allow_all"))[
        "type"
    ] == "edit_control.accepted"
    assert (await client.request("command_control.set", command_control="permissive"))[
        "type"
    ] == "command_control.accepted"


async def test_client_error_response_raises(client: ValidatorClient) -> None:
    await client.hello()
    with pytest.raises(ProtocolError, match="empty_prompt"):
        await client.request("prompt.submit", text="")


async def test_client_transcript_records_traffic(real_server: TestServer, tmp_path: Path) -> None:
    transcript = Transcript(tmp_path / "t.jsonl")
    c = ValidatorClient(f"ws://127.0.0.1:{real_server.port}/ws", transcript, ScriptedUser())
    await c.connect()
    try:
        await c.hello()
    finally:
        await c.close()

    kinds = {(e.direction, e.kind) for e in transcript.entries}
    assert ("send", "request") in kinds
    assert ("recv", "response") in kinds
    # JSONL file mirrors the in-memory entries.
    lines = (tmp_path / "t.jsonl").read_text().strip().splitlines()
    assert len(lines) == len(transcript.entries)
    assert json.loads(lines[0])["seq"] == 0


# ---------------------------------------------------------------------------
# ValidatorClient against a stub server: simulated interactions + turn end
# ---------------------------------------------------------------------------


class _StubServer:
    """Bare WS endpoint the tests drive frame by frame."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Envelope] = asyncio.Queue()
        self.ws: web.WebSocketResponse | None = None
        self._connected = asyncio.Event()

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self._connected.set()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                self.incoming.put_nowait(Envelope.from_json(str(msg.data)))
        return ws

    async def wait_connected(self) -> web.WebSocketResponse:
        await asyncio.wait_for(self._connected.wait(), timeout=_RECV_TIMEOUT)
        assert self.ws is not None
        return self.ws

    async def send(self, env: Envelope) -> None:
        ws = await self.wait_connected()
        await ws.send_str(env.to_json())

    async def recv(self) -> Envelope:
        return await asyncio.wait_for(self.incoming.get(), timeout=_RECV_TIMEOUT)


@pytest.fixture
async def stub() -> AsyncGenerator[tuple[_StubServer, TestServer], None]:
    stub_server = _StubServer()
    app = web.Application()
    app.router.add_get("/ws", stub_server.handler)
    srv = TestServer(app)
    await srv.start_server()
    yield stub_server, srv
    await srv.close()


def _state_event(phase: str) -> Envelope:
    return Envelope.make_event("state", {"phase": phase})


async def test_client_answers_question_request_and_logs_it(
    stub: tuple[_StubServer, TestServer],
) -> None:
    stub_server, srv = stub
    transcript = Transcript()
    user = ScriptedUser(question_script=[[QuestionAnswer(selected=["SQLite"])]])
    client = ValidatorClient(f"ws://127.0.0.1:{srv.port}/ws", transcript, user)
    await client.connect()
    try:
        request = Envelope(
            kind="request",
            payload={
                "type": "prompt.question",
                "tool_call_id": "toolu_1",
                "questions": [{"question": "DB?", "options": ["PostgreSQL", "SQLite"]}],
            },
        )
        await stub_server.send(request)

        reply = await stub_server.recv()
        assert reply.kind == "response"
        assert reply.correlation_id == request.id
        assert reply.payload["type"] == "prompt.question.response"
        answers = cast(list[dict[str, object]], reply.payload["answers"])
        assert answers[0]["selected"] == ["SQLite"]

        interactions = transcript.interactions()
        assert len(interactions) == 1
        assert interactions[0].payload["interaction"] == "prompt.question"
    finally:
        await client.close()


async def test_client_answers_api_key_request(stub: tuple[_StubServer, TestServer]) -> None:
    stub_server, srv = stub
    user = ScriptedUser(api_keys={"anthropic": "sk-test"})
    client = ValidatorClient(f"ws://127.0.0.1:{srv.port}/ws", Transcript(), user)
    await client.connect()
    try:
        request = Envelope(
            kind="request", payload={"type": "api_key.request", "vendor": "anthropic"}
        )
        await stub_server.send(request)
        reply = await stub_server.recv()
        assert reply.correlation_id == request.id
        assert reply.payload == {"api_key": "sk-test"}
    finally:
        await client.close()


async def test_client_turn_end_waits_for_running_then_rest(
    stub: tuple[_StubServer, TestServer],
) -> None:
    stub_server, srv = stub
    client = ValidatorClient(f"ws://127.0.0.1:{srv.port}/ws", Transcript(), ScriptedUser())
    await client.connect()
    try:
        client.begin_turn()
        waiter = asyncio.create_task(
            client.wait_turn_end(timeout=_RECV_TIMEOUT, settle_seconds=0.05)
        )
        # A resting phase alone must not end the turn — running was never seen.
        await stub_server.send(_state_event("awaiting_user"))
        await asyncio.sleep(0.2)
        assert not waiter.done()

        await stub_server.send(_state_event("running"))
        await stub_server.send(_state_event("awaiting_user"))
        assert await asyncio.wait_for(waiter, timeout=_RECV_TIMEOUT) == "awaiting_user"
    finally:
        await client.close()


async def test_client_turn_end_streams_are_assembled(
    stub: tuple[_StubServer, TestServer],
) -> None:
    stub_server, srv = stub
    transcript = Transcript()
    client = ValidatorClient(f"ws://127.0.0.1:{srv.port}/ws", transcript, ScriptedUser())
    await client.connect()
    try:
        stream_id = "stream-1"
        await stub_server.send(Envelope.make_stream_chunk(stream_id, "Hello "))
        await stub_server.send(Envelope.make_stream_chunk(stream_id, "world"))
        await stub_server.send(Envelope.make_stream_end(stream_id))
        # The turn-end machinery is exercised elsewhere; here just let the
        # pump drain, then check the assembled note.
        await asyncio.sleep(0.2)
        assert transcript.assistant_text() == "Hello world"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# ensure_local_llms_installed
# ---------------------------------------------------------------------------


class _FakeInstallClient:
    """Records ``local_llm.install`` sends without touching a real socket."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(
        self, msg_type: str, payload: dict[str, object] | None = None, **fields: object
    ) -> None:
        assert msg_type == "local_llm.install"
        self.sent.append(str(fields["name"]))


def _registry_entry(name: str, *, installed: bool, kind: str = "hardcoded_hf") -> dict[str, object]:
    return {"name": name, "kind": kind, "installed": installed}


def _file_entry(status: str, *, role: str = "main", error: str = "") -> dict[str, object]:
    return {
        "filename": "model.gguf",
        "role": role,
        "repo_id": "org/repo",
        "status": status,
        "error": error,
    }


def _write_manager_state(models_dir: Path, records: dict[str, dict[str, object]]) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "manager-state.json").write_text(json.dumps(records), encoding="utf-8")


async def test_ensure_llms_already_installed_skips_download(tmp_path: Path) -> None:
    client = _FakeInstallClient()
    registry = [
        _registry_entry("model-a", installed=True),
        _registry_entry("model-b", installed=True),
    ]
    await ensure_local_llms_installed(
        cast(ValidatorClient, client), tmp_path, registry, ["model-a", "model-b"]
    )
    assert client.sent == []


async def test_ensure_llms_unknown_name_raises(tmp_path: Path) -> None:
    client = _FakeInstallClient()
    with pytest.raises(LocalModelUnavailableError, match="Unknown local model"):
        await ensure_local_llms_installed(cast(ValidatorClient, client), tmp_path, [], ["nope"])
    assert client.sent == []


async def test_ensure_llms_undownloadable_kind_raises(tmp_path: Path) -> None:
    client = _FakeInstallClient()
    registry = [_registry_entry("server-model", installed=False, kind="custom_server_url")]
    with pytest.raises(LocalModelUnavailableError, match="cannot be auto-downloaded"):
        await ensure_local_llms_installed(
            cast(ValidatorClient, client), tmp_path, registry, ["server-model"]
        )
    assert client.sent == []


async def test_ensure_llms_downloads_missing_and_polls_to_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator_models, "_POLL_SECONDS", 0.01)
    client = _FakeInstallClient()
    registry = [_registry_entry("model-a", installed=False)]
    models_dir = tmp_path / "llama.cpp" / "models"

    async def _finish_after_first_poll() -> None:
        await asyncio.sleep(0.03)
        _write_manager_state(models_dir, {"model-a": {"files": [_file_entry("completed")]}})

    finisher = asyncio.create_task(_finish_after_first_poll())
    try:
        await ensure_local_llms_installed(
            cast(ValidatorClient, client), tmp_path, registry, ["model-a"], poll_timeout=5.0
        )
    finally:
        await finisher
    assert client.sent == ["model-a"]


async def test_ensure_llms_failed_download_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator_models, "_POLL_SECONDS", 0.01)
    client = _FakeInstallClient()
    registry = [_registry_entry("model-a", installed=False)]
    models_dir = tmp_path / "llama.cpp" / "models"
    _write_manager_state(
        models_dir, {"model-a": {"files": [_file_entry("failed", error="401 unauthorized")]}}
    )

    with pytest.raises(LocalModelUnavailableError, match="401 unauthorized"):
        await ensure_local_llms_installed(
            cast(ValidatorClient, client), tmp_path, registry, ["model-a"], poll_timeout=5.0
        )


async def test_ensure_llms_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator_models, "_POLL_SECONDS", 0.01)
    client = _FakeInstallClient()
    registry = [_registry_entry("model-a", installed=False)]

    with pytest.raises(LocalModelUnavailableError, match="Timed out"):
        await ensure_local_llms_installed(
            cast(ValidatorClient, client), tmp_path, registry, ["model-a"], poll_timeout=0.05
        )


async def test_ensure_llms_respects_custom_llm_models_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator_models, "_POLL_SECONDS", 0.01)
    client = _FakeInstallClient()
    registry = [_registry_entry("model-a", installed=False)]
    custom_dir = tmp_path / "elsewhere"
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "settings.json").write_text(
        json.dumps({"llm_models_dir": str(custom_dir)}), encoding="utf-8"
    )
    _write_manager_state(custom_dir, {"model-a": {"files": [_file_entry("completed")]}})

    await ensure_local_llms_installed(
        cast(ValidatorClient, client), tmp_path, registry, ["model-a"], poll_timeout=5.0
    )
    assert client.sent == ["model-a"]

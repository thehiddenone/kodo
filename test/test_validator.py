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
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.llms import get_local_registry
from kodo.project import kodo_user_dir
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
    VLLMProxyError,
    VLLMUserProxy,
    clone_kodo_home,
    ensure_local_llms_installed,
)
from kodo.validator import _evaluate as validator_evaluate
from kodo.validator import _models as validator_models

_RECV_TIMEOUT = 5.0


def _can_create_symlinks() -> bool:
    """Whether this process can create filesystem symlinks.

    On Windows, ``Path.symlink_to`` needs either Administrator privileges or
    Developer Mode enabled (``SeCreateSymbolicLinkPrivilege``) — absent
    either, it raises ``OSError: [WinError 1314]``. ``clone_kodo_home``
    documents this as a real, expected failure mode rather than something to
    work around, so the tests that exercise its symlinking behavior skip
    (instead of failing) when the current process lacks that privilege.
    """
    with tempfile.TemporaryDirectory() as raw_dir:
        d = Path(raw_dir)
        target = d / "target"
        target.write_text("x", encoding="utf-8")
        try:
            (d / "link").symlink_to(target)
        except OSError:
            return False
        return True


_SYMLINKS_SUPPORTED = _can_create_symlinks()
_NO_SYMLINK_PRIVILEGE_REASON = (
    "Creating symlinks requires elevated privilege/Developer Mode on this platform"
)


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "test-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    # See test_server_integration.py's _temp_home: keep server boot fully
    # offline instead of racing a real titler llama-server download/spin-up
    # every test.
    async def _no_op_start_titling(kodo_dir: Path) -> None:
        return None

    async def _no_op_generate_title(text: str) -> None:
        return None

    monkeypatch.setattr(_app_module, "start_titling", _no_op_start_titling)
    monkeypatch.setattr(_titling_module, "generate_title", _no_op_generate_title)
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


@pytest.mark.skipif(not _SYMLINKS_SUPPORTED, reason=_NO_SYMLINK_PRIVILEGE_REASON)
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


@pytest.mark.skipif(not _SYMLINKS_SUPPORTED, reason=_NO_SYMLINK_PRIVILEGE_REASON)
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


# ---------------------------------------------------------------------------
# VLLMUserProxy (phase 2): questions answered via llm.select / llm.complete
# ---------------------------------------------------------------------------


class _FakeVLLMClient:
    """Duck-typed ValidatorClient: records requests, plays canned completions."""

    def __init__(self, completions: list[str]) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._completions = list(completions)

    async def request(
        self,
        msg_type: str,
        payload: dict[str, object] | None = None,
        *,
        session_scoped: bool = True,
        timeout: float = 30.0,
        check: bool = True,
        **fields: object,
    ) -> dict[str, object]:
        body: dict[str, object] = {**(payload or {}), **fields}
        self.calls.append((msg_type, body))
        if msg_type == "llm.select":
            return {"type": "llm.select.done", "ok": True, "model": body.get("name")}
        assert msg_type == "llm.complete"
        return {"type": "llm.complete.done", "ok": True, "text": self._completions.pop(0)}


_QUESTION_PAYLOAD: dict[str, object] = {
    "type": "prompt.question",
    "questions": [{"question": "DB?", "kind": "choice", "options": ["PostgreSQL", "SQLite"]}],
}


def _make_proxy(client: _FakeVLLMClient, **kwargs: object) -> VLLMUserProxy:
    proxy = VLLMUserProxy(
        user_proxy_prompt="UPP",
        llm_under_test="lut-model",
        validation_llm="judge-model",
        **kwargs,  # type: ignore[arg-type]
    )
    proxy.bind(cast(ValidatorClient, client), Transcript())
    proxy.set_task_prompt("Build a parser")
    return proxy


async def test_vllm_proxy_switches_completes_and_switches_back() -> None:
    fake = _FakeVLLMClient([json.dumps({"answers": [{"selected": ["SQLite"], "free_text": ""}]})])
    proxy = _make_proxy(fake)

    reply = await proxy.answer_questions(_QUESTION_PAYLOAD)

    assert reply["type"] == "prompt.question.response"
    assert reply["answers"] == [{"selected": ["SQLite"], "free_text": None}]
    assert [(t, b.get("name")) for t, b in fake.calls] == [
        ("llm.select", "judge-model"),
        ("llm.complete", None),
        ("llm.select", "lut-model"),
    ]
    complete_body = fake.calls[1][1]
    assert complete_body["system"] == "UPP"
    prompt = cast(str, complete_body["prompt"])
    assert "Build a parser" in prompt
    assert "SQLite" in prompt
    schema = cast(dict[str, object], complete_body["json_schema"])
    answers_schema = cast(
        dict[str, object], cast(dict[str, object], schema["properties"])["answers"]
    )
    assert answers_schema["minItems"] == 1
    assert answers_schema["maxItems"] == 1
    assert proxy.failure is None


async def test_vllm_proxy_folds_stray_selection_into_free_text() -> None:
    fake = _FakeVLLMClient([json.dumps({"answers": [{"selected": ["MongoDB"], "free_text": ""}]})])
    proxy = _make_proxy(fake)
    reply = await proxy.answer_questions(_QUESTION_PAYLOAD)
    assert reply["answers"] == [{"selected": [], "free_text": "MongoDB"}]


async def test_vllm_proxy_retries_then_fails_but_restores_lut() -> None:
    fake = _FakeVLLMClient(["not json", "{}", json.dumps({"answers": []})])
    proxy = _make_proxy(fake, max_attempts=3)

    with pytest.raises(VLLMProxyError):
        await proxy.answer_questions(_QUESTION_PAYLOAD)

    assert proxy.failure is not None
    completes = [c for c in fake.calls if c[0] == "llm.complete"]
    assert len(completes) == 3
    # The finally-block switch-back must have run despite the failure.
    assert fake.calls[-1][0] == "llm.select"
    assert fake.calls[-1][1]["name"] == "lut-model"


async def test_vllm_proxy_delegates_other_gates_to_base() -> None:
    base = ScriptedUser(permission_action="deny", permission_feedback="nope")
    proxy = VLLMUserProxy(
        user_proxy_prompt="UPP",
        llm_under_test="lut-model",
        validation_llm="judge-model",
        base=base,
    )
    reply = await proxy.answer_permission({"type": "prompt.permission"})
    assert reply["action"] == "deny"
    assert (await proxy.answer_approval({"type": "prompt.approval"}))["action"] == "agree"


# ---------------------------------------------------------------------------
# Evaluation (phase 2): score extraction from judge text
# ---------------------------------------------------------------------------


def test_parse_score_accepts_plain_fenced_and_embedded_json() -> None:
    assert validator_evaluate._parse_score('{"score": 87, "report": "ok"}') == (87.0, "ok")

    fenced = 'Verdict below.\n```json\n{"score": 55.5, "report": "meh"}\n```\nDone.'
    assert validator_evaluate._parse_score(fenced) == (55.5, "meh")

    embedded = 'Thinking... {"score": 10, "report": "weak"} — that is my verdict.'
    assert validator_evaluate._parse_score(embedded) == (10.0, "weak")


def test_parse_score_rejects_malformed_verdicts() -> None:
    for text in (
        "no json here",
        '{"score": true, "report": "x"}',
        '{"score": 130, "report": "x"}',
        '{"score": "high", "report": "x"}',
        '{"report": "x"}',
        "[1, 2, 3]",
    ):
        with pytest.raises(ValueError):
            validator_evaluate._parse_score(text)


def _tool_call(tool_name: str, rows: list[dict[str, object]] | None) -> dict[str, object]:
    """A ``Transcript.tool_calls`` entry (prep payload + merged detail)."""
    detail = {"tool_call_id": "tc1", "rows": rows} if rows is not None else None
    return {"tool_name": tool_name, "tool_call_id": "tc1", "detail": detail}


def _rows(score: object, report: object) -> list[dict[str, object]]:
    """submit_evaluation detail rows: input pair then the tool's output echo."""
    return [
        {"name": "score", "value": str(score), "source": "input", "visibility": "always"},
        {"name": "report", "value": str(report), "source": "input", "visibility": "visible"},
        {"name": "status", "value": "recorded", "source": "output", "visibility": "always"},
        {"name": "score", "value": str(score), "source": "output", "visibility": "always"},
        {"name": "report", "value": str(report), "source": "output", "visibility": "visible"},
    ]


def test_verdict_from_tool_calls_reads_submit_evaluation() -> None:
    calls = [
        _tool_call("read_file", None),
        _tool_call("submit_evaluation", _rows("82.5", "solid but thin validation")),
    ]
    assert validator_evaluate._verdict_from_tool_calls(calls) == (
        82.5,
        "solid but thin validation",
    )


def test_verdict_from_tool_calls_prefers_last_call() -> None:
    calls = [
        _tool_call("submit_evaluation", _rows("10", "first pass")),
        _tool_call("submit_evaluation", _rows("90", "on reflection, good")),
    ]
    assert validator_evaluate._verdict_from_tool_calls(calls) == (90.0, "on reflection, good")


def test_verdict_from_tool_calls_returns_none_without_tool() -> None:
    assert validator_evaluate._verdict_from_tool_calls([_tool_call("find_files", None)]) is None
    assert validator_evaluate._verdict_from_tool_calls([]) is None
    # A submit_evaluation call whose score row never parses yields no verdict.
    bad = [_tool_call("submit_evaluation", _rows("not-a-number", "r"))]
    assert validator_evaluate._verdict_from_tool_calls(bad) is None


def test_coerce_row_score_bounds() -> None:
    assert validator_evaluate._coerce_row_score("0") == 0.0
    assert validator_evaluate._coerce_row_score("100") == 100.0
    assert validator_evaluate._coerce_row_score("50.5") == 50.5
    for bad in ("101", "-1", "nan", "abc", None, ""):
        assert validator_evaluate._coerce_row_score(bad) is None


# ---------------------------------------------------------------------------
# Pre-flight model check (missing_local_llms) — disk-only, no server/download
# ---------------------------------------------------------------------------


def test_missing_local_llms_disk_check(tmp_path: Path) -> None:
    models = tmp_path / "llama.cpp" / "models"
    models.mkdir(parents=True)
    state = {
        "installed": {"files": [{"role": "main", "status": "completed"}]},
        "sharded": {
            "files": [
                {"role": "shard", "status": "completed"},
                {"role": "shard", "status": "completed"},
            ]
        },
        "pending": {"files": [{"role": "main", "status": "downloading"}]},
        "failed": {"files": [{"role": "main", "status": "failed"}]},
    }
    (models / "manager-state.json").write_text(json.dumps(state), encoding="utf-8")

    assert validator_models.missing_local_llms(tmp_path, ["installed", "sharded"]) == []
    assert validator_models.missing_local_llms(
        tmp_path, ["installed", "pending", "failed", "absent", "absent"]
    ) == ["pending", "failed", "absent"]


def test_missing_local_llms_no_state_file(tmp_path: Path) -> None:
    # No manager-state.json at all → every name is missing.
    assert validator_models.missing_local_llms(tmp_path, ["a", "b"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Server child environment (HOME redirect + global HF cache + titler offline)
# ---------------------------------------------------------------------------


def test_build_child_env_redirects_home_and_pins_hf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.validator import _server as validator_server

    real_home = tmp_path / "real-home"
    run_home = tmp_path / "run-home"
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("HF_HOME", raising=False)

    env = validator_server.build_child_env(run_home)

    # HOME/USERPROFILE point at the throwaway run home.
    assert env["HOME"] == str(run_home)
    assert env["USERPROFILE"] == str(run_home)
    # HF cache is pinned to the *real* global location, not under the run home.
    assert env["HF_HOME"] == str(real_home / ".cache" / "huggingface")
    assert str(run_home) not in env["HF_HOME"]


def test_build_child_env_respects_existing_hf_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.validator import _server as validator_server

    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("HF_HOME", "/some/global/hf")

    env = validator_server.build_child_env(tmp_path / "run-home")

    assert env["HF_HOME"] == "/some/global/hf"


# ---------------------------------------------------------------------------
# Scenario selector resolver
# ---------------------------------------------------------------------------

_FAKE_SCENARIO = (
    "from kodo.validator import Scenario\n"
    "SCENARIO = Scenario(name={name!r}, prompts=['p'], "
    "llm_under_test='lut-a', validation_llm='vllm-b')\n"
)


def _write_fake_scenarios(root: Path) -> None:
    (root / "fam").mkdir(parents=True)
    (root / "fam" / "a.py").write_text(_FAKE_SCENARIO.format(name="a"), encoding="utf-8")
    (root / "fam" / "b.py").write_text(_FAKE_SCENARIO.format(name="b"), encoding="utf-8")
    (root / "top.py").write_text(_FAKE_SCENARIO.format(name="top"), encoding="utf-8")
    # Private/dunder files are not scenarios.
    (root / "_helper.py").write_text("SCENARIO = None\n", encoding="utf-8")


def test_resolve_selectors_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kodo.validator import scenarios as scn

    _write_fake_scenarios(tmp_path)
    monkeypatch.setattr(scn, "_SCENARIOS_DIR", tmp_path)

    assert set(scn.scenario_ids()) == {"fam.a", "fam.b", "top"}
    # Submodule expands to all its files, sorted.
    assert [i for i, _ in scn.resolve_selectors(["fam"])] == ["fam.a", "fam.b"]
    # A single scenario by dotted id.
    assert [i for i, _ in scn.resolve_selectors(["fam.a"])] == ["fam.a"]
    # 'all' selects everything; dedup keeps a repeat from running twice.
    assert len(scn.resolve_selectors(["all"])) == 3
    assert [i for i, _ in scn.resolve_selectors(["fam", "fam.a"])] == ["fam.a", "fam.b"]
    # Unknown selector fails loudly.
    with pytest.raises(scn.ScenarioResolutionError):
        scn.resolve_selectors(["nope"])


def test_resolve_selectors_shipped_scenarios() -> None:
    from kodo.validator import scenarios as scn

    ids = scn.scenario_ids()
    assert "qwen35-9b.tictactoe_console" in ids
    assert "qwen36-27b.tictactoe_upp" in ids
    resolved = scn.resolve_selectors(["all"])
    assert {i for i, _ in resolved} == set(ids)
    names = {s.name for _, s in resolved}
    assert {"tictactoe-console", "tictactoe-upp"} <= names


def test_shipped_scenarios_share_prompts_via_registry() -> None:
    """Both tictactoe scenarios pull one shared UPP/RVP and differ only by task."""
    from kodo.validator import scenarios as scn
    from kodo.validator.prompts import PROMPTS

    by_name = {s.name: s for _, s in scn.resolve_selectors(["all"])}
    detailed = by_name["tictactoe-console"]
    sparse = by_name["tictactoe-upp"]

    # One UPP and one RVP, shared verbatim across both variants.
    assert detailed.result_validation_prompt == sparse.result_validation_prompt
    assert detailed.result_validation_prompt == PROMPTS.get("tictactoe/rvp")
    assert detailed.user_proxy_prompt == sparse.user_proxy_prompt
    assert detailed.user_proxy_prompt == PROMPTS.get("tictactoe/upp")
    # The scenarios diverge only in the task prompt (detailed vs. sparse).
    assert detailed.prompts == [PROMPTS.get("tictactoe/detailed_task")]
    assert sparse.prompts == [PROMPTS.get("tictactoe/sparse_task")]
    assert detailed.prompts != sparse.prompts


def test_prompt_registry_resolution_and_guards() -> None:
    from kodo.validator.prompts import PROMPTS, PromptNotFoundError

    # Submodule name resolves; a trailing ``.md`` is tolerated and equivalent.
    assert PROMPTS.get("tictactoe/rvp") == PROMPTS.get("tictactoe/rvp.md")
    assert set(PROMPTS.names()) >= {
        "tictactoe/detailed_task",
        "tictactoe/sparse_task",
        "tictactoe/upp",
        "tictactoe/rvp",
    }
    # A missing prompt raises, and the message lists what is available.
    with pytest.raises(PromptNotFoundError, match="Available:"):
        PROMPTS.get("tictactoe/nope")
    # Empty, traversal, and absolute names are rejected before touching disk.
    for bad in ("", "..", "../secret", "/etc/passwd", "tictactoe/../../secret"):
        with pytest.raises(PromptNotFoundError):
            PROMPTS.get(bad)


# ---------------------------------------------------------------------------
# llm.select / llm.complete against the real in-process server
# ---------------------------------------------------------------------------


async def test_llm_select_unknown_model_replies_error(client: ValidatorClient) -> None:
    resp = await client.request(
        "llm.select", name="no-such-model", session_scoped=False, check=False
    )
    assert resp["type"] == "llm.select.done"
    assert resp["ok"] is False
    assert "Unknown local model" in str(resp["error"])


async def test_llm_select_persists_selection_even_when_start_fails(
    client: ValidatorClient, _temp_home: Path
) -> None:
    # Any hardcoded registry entry: known to the registry, but llama.cpp is
    # not installed in the temp home, so the start step fails — while the
    # settings write must already have happened (documented semantics).
    name = next(iter(get_local_registry(kodo_user_dir())))
    resp = await client.request("llm.select", name=name, session_scoped=False, check=False)
    assert resp["type"] == "llm.select.done"
    assert resp["ok"] is False

    settings = json.loads(
        (_temp_home / ".kodo" / "etc" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["mode"] == "local"
    assert settings["models"]["local"] == name


async def test_llm_complete_requires_prompt(client: ValidatorClient) -> None:
    resp = await client.request("llm.complete", session_scoped=False, check=False)
    assert resp["type"] == "llm.complete.done"
    assert resp["ok"] is False
    assert "prompt is required" in str(resp["error"])


async def test_llm_complete_fails_cleanly_without_llama(client: ValidatorClient) -> None:
    resp = await client.request("llm.complete", prompt="hello", session_scoped=False, check=False)
    assert resp["type"] == "llm.complete.done"
    assert resp["ok"] is False
    assert resp["error"]

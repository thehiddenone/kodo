"""The headless server mode: ``kodo-server --headless-sandbox DIR [--llama-url URL]``
(doc/HEADLESS.md).

Covers the two flags' parsing, the startup steps headless mode must skip
(they touch state shared with the user's own kodo), and — end to end, through
a real in-process server and a real session — that every tool call is judged
by the sandbox posture: a scripted model asks for writes outside the sandbox
and a mutating git command, and the test checks both were refused and nothing
was written.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.llms import LLMRouting, TokenDelta, ToolCallEvent, TurnEnd, Usage
from kodo.runtime import WorkflowEngine
from kodo.runtime._engine import _titling as _titling_module
from kodo.server import Config, create_app
from kodo.server import _app as _app_module

_RECV_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    async def _no_op_generate_title(text: str) -> None:
        return None

    monkeypatch.setattr(_titling_module, "generate_title", _no_op_generate_title)
    return home


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def test_headless_flags_default_off() -> None:
    config = Config.from_args([])
    assert config.headless_sandbox is None
    assert config.llama_url is None


def test_headless_flags_parse(sandbox: Path) -> None:
    config = Config.from_args(
        ["--headless-sandbox", str(sandbox), "--llama-url", "http://host.docker.internal:8090/"]
    )
    assert config.headless_sandbox == sandbox.resolve()
    assert config.llama_url == "http://host.docker.internal:8090"


def test_llama_url_requires_headless_sandbox() -> None:
    with pytest.raises(SystemExit):
        Config.from_args(["--llama-url", "http://127.0.0.1:8090"])


def test_headless_sandbox_must_be_a_directory(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        Config.from_args(["--headless-sandbox", str(tmp_path / "missing")])


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def patch(self, monkeypatch: pytest.MonkeyPatch, name: str, result: object = None) -> None:
        def _record(*_a: object, **_k: object) -> object:
            self.calls.append(name)
            return result

        async def _record_async(*_a: object, **_k: object) -> object:
            self.calls.append(name)
            return result

        original = getattr(_app_module, name)
        monkeypatch.setattr(
            _app_module,
            name,
            _record_async if asyncio.iscoroutinefunction(original) else _record,
        )


@pytest.fixture
def startup_recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    recorder.patch(monkeypatch, "ensure_all_utils")
    recorder.patch(monkeypatch, "find_running_server")
    recorder.patch(monkeypatch, "purge_unknown_local_models", ())
    recorder.patch(monkeypatch, "start_titling")
    recorder.patch(monkeypatch, "run_openrouter_catalog_refresh_loop")
    return recorder


async def test_headless_startup_skips_shared_state_steps(
    sandbox: Path, startup_recorder: _Recorder
) -> None:
    server = TestServer(create_app(Config(headless_sandbox=sandbox)))
    await server.start_server()
    await server.close()
    assert startup_recorder.calls == ["ensure_all_utils"]


async def test_interactive_startup_still_runs_them(startup_recorder: _Recorder) -> None:
    server = TestServer(create_app(Config()))
    await server.start_server()
    await server.close()
    assert "purge_unknown_local_models" in startup_recorder.calls
    assert "find_running_server" in startup_recorder.calls


# ---------------------------------------------------------------------------
# End to end: a sandboxed session
# ---------------------------------------------------------------------------


class _ScriptedPlugin:
    """Replays one scripted tool call per LLM call, then ends the turn."""

    name = "scripted"

    def __init__(self, tool_calls: list[tuple[str, dict[str, object]]]) -> None:
        self.__tool_calls = list(tool_calls)
        self.seen_messages: list[str] = []
        self.finished = asyncio.Event()

    async def stream_query(self, **kwargs: object) -> AsyncIterator[object]:
        self.seen_messages.append(str(kwargs.get("messages")))
        usage = Usage(
            input_tokens=1,
            output_tokens=1,
            cache_write_tokens=0,
            cache_read_tokens=0,
            model="fake-model",
        )
        if self.__tool_calls:
            name, tool_input = self.__tool_calls.pop(0)
            yield ToolCallEvent(
                tool_use_id=f"tu_{len(self.seen_messages)}", tool_name=name, tool_input=tool_input
            )
            yield TurnEnd(usage=usage, stop_reason="tool_use")
            return
        yield TokenDelta(text="done")
        yield TurnEnd(usage=usage, stop_reason="end_turn")
        self.finished.set()

    async def cancel(self, stream_id: str) -> None:
        return None


async def _recv_response(ws: aiohttp.ClientWebSocketResponse, correlation_id: str) -> Envelope:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RECV_TIMEOUT
    while True:
        msg = await asyncio.wait_for(ws.receive(), timeout=deadline - loop.time())
        env = Envelope.from_json(str(msg.data))
        if env.kind == "response" and env.correlation_id == correlation_id:
            return env


async def _request(
    ws: aiohttp.ClientWebSocketResponse, session_id: str | None, msg_type: str, **payload: object
) -> dict[str, object]:
    body: dict[str, object] = {"type": msg_type, **payload}
    if session_id is not None:
        body["session_id"] = session_id
    env = Envelope(kind="request", payload=body)
    await ws.send_str(env.to_json())
    return (await _recv_response(ws, env.id)).payload


async def test_sandboxed_session_refuses_outside_writes_and_git_mutation(
    sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "escape.txt"
    inside = sandbox / "hello.py"
    plugin = _ScriptedPlugin(
        [
            ("create_file", {"intent": "escape", "path": str(outside), "content": "x"}),
            ("run_command", {"intent": "commit", "command": "git commit -m x", "timeout": 5}),
            ("create_file", {"intent": "ok", "path": str(inside), "content": "print('hi')\n"}),
        ]
    )

    async def _resolve_plugin(
        self: WorkflowEngine, capability: str, force_model_key: str | None = None
    ) -> tuple[object, str, LLMRouting]:
        return plugin, "fake-model", LLMRouting(residence="local")

    monkeypatch.setattr(WorkflowEngine, "_resolve_plugin", _resolve_plugin)

    server = TestServer(create_app(Config(headless_sandbox=sandbox)))
    await server.start_server()
    http = aiohttp.ClientSession()
    try:
        ws = await http.ws_connect(f"http://127.0.0.1:{server.port}/ws")
        ack = await _request(ws, None, "hello", client="test", version="1", window_id="w1")
        session_id = str(ack["session_id"])
        await _request(
            ws,
            session_id,
            "workspace.folders",
            physical_root=str(sandbox),
            folders={sandbox.name: str(sandbox)},
        )
        await _request(ws, session_id, "mode.set", autonomous=True)
        await _request(ws, session_id, "prompt.submit", text="go")

        async def _drain() -> None:
            while not plugin.finished.is_set():
                await ws.receive()

        await asyncio.wait_for(_drain(), timeout=30)
        await ws.close()
    finally:
        await http.close()
        await server.close()

    # The last LLM call carries the whole history: both refusals are in it.
    assert plugin.seen_messages[-1].count("Blocked by the headless sandbox") == 2
    assert not outside.exists()
    assert inside.read_text(encoding="utf-8") == "print('hi')\n"

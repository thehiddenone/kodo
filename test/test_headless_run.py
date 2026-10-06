"""``HeadlessRun`` end to end against fakes (doc/HEADLESS.md): no real process.

``asyncio.create_subprocess_exec`` is replaced by a spawner that, instead of
starting ``kodo-server``, brings up a scripted in-process WebSocket server on
the very port the run asked for; ``subprocess.Popen``/``subprocess.run`` and
``urllib.request.urlopen`` stand in for a spawned ``kodo-llama-server`` and its
``/health`` endpoint. Every test then checks what the run reports: the
outcome, the error, the events on stdout, and what it leaves on disk.
"""

from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import TracebackType

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kodo.common import Envelope
from kodo.headless import (
    EventSink,
    HeadlessOptions,
    HeadlessRun,
    RunOutcome,
    RunResult,
    install_signal_handlers,
)

_SESSION_ID = "s-1"


# ---------------------------------------------------------------------------
# Fake kodo-server
# ---------------------------------------------------------------------------


class _FakeKodo:
    """A scripted kodo server that answers setup and plays one turn.

    ``turn`` is one of ``complete`` (running → text → awaiting_user),
    ``error`` (a runtime error before resting), ``hang`` (never rests until a
    ``stop`` arrives) and ``drop`` (closes the socket mid-turn).
    """

    def __init__(
        self,
        *,
        turn: str = "complete",
        agents: tuple[str, ...] = ("kodo_problem_solver",),
        thinking_ok: bool = True,
        failing_request: str | None = None,
    ) -> None:
        self.turn = turn
        self.agents = agents
        self.thinking_ok = thinking_ok
        self.failing_request = failing_request
        self.received: list[dict[str, object]] = []

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            env = Envelope.from_json(str(msg.data))
            if env.kind != "request":
                continue
            self.received.append(env.payload)
            kind = str(env.payload.get("type"))
            await ws.send_str(Envelope.make_response(env.id, self.__reply(kind)).to_json())
            if kind == "prompt.submit":
                await self.__play_turn(ws)
            elif kind == "stop":
                await _event(ws, type="state", phase="stopped")
        return ws

    def __reply(self, kind: str) -> dict[str, object]:
        if kind == self.failing_request:
            return {"type": "error", "code": "bad_request", "message": f"{kind} refused"}
        if kind == "hello":
            return {"type": "hello.ack", "session_id": _SESSION_ID, "state": {"phase": "intake"}}
        if kind == "top_agents.list":
            return {"type": "top_agents.list.ack", "agents": [{"name": a} for a in self.agents]}
        if kind == "thinking_level.set":
            return {"type": "thinking_level.set.ack", "ok": self.thinking_ok}
        return {"type": f"{kind}.ack"}

    async def __play_turn(self, ws: web.WebSocketResponse) -> None:
        await _event(ws, type="state", phase="running")
        if self.turn == "hang":
            return
        if self.turn == "drop":
            await ws.close()
            return
        await _event(ws, type="llm.turn_start", agent="problem_solver", model="m")
        if self.turn == "error":
            await _event(ws, type="error", code="runtime_error", message="model exploded")
        else:
            chunk = Envelope(
                kind="stream_chunk",
                correlation_id="t",
                payload={"type": "agent.tokens", "text": "all done"},
            )
            await ws.send_str(chunk.to_json())
            await ws.send_str(Envelope(kind="stream_end", correlation_id="t", payload={}).to_json())
            await _event(
                ws,
                type="usage.update",
                cumulative_input_tokens=12,
                cumulative_input_tokens_uncached=10,
                cumulative_output_tokens=5,
                cumulative_usd=0.25,
                last_call_tokens={"input": 12, "output": 5, "cache_read": 2, "cache_write": 0},
                model="m",
                agent="problem_solver",
                usd_cost=0.25,
            )
        await _event(ws, type="state", phase="awaiting_user")


async def _event(ws: web.WebSocketResponse, **payload: object) -> None:
    await ws.send_str(Envelope.make_event(str(payload.pop("type")), payload).to_json())


class _FakeServerProcess:
    """What ``asyncio.create_subprocess_exec`` returns in place of kodo-server."""

    def __init__(self, server: TestServer | None, returncode: int | None) -> None:
        self.__server = server
        self.returncode = returncode
        self.pid = 4242

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        if self.__server is not None:
            await self.__server.close()
            self.__server = None
        self.returncode = 0
        return 0


class _Spawner:
    """Stands in for ``asyncio.create_subprocess_exec`` launching kodo-server."""

    def __init__(
        self, kodo: _FakeKodo, *, exit_code: int | None = None, session_log: bool = True
    ) -> None:
        self.__kodo = kodo
        self.__exit_code = exit_code
        self.__session_log = session_log
        self.args: list[str] = []
        self.servers: list[TestServer] = []

    async def __call__(self, *args: object, **kwargs: object) -> _FakeServerProcess:
        self.args = [str(a) for a in args]
        if self.__exit_code is not None:
            return _FakeServerProcess(None, self.__exit_code)
        env = kwargs["env"]
        assert isinstance(env, dict)
        if self.__session_log:
            session_dir = Path(str(env["HOME"])) / ".kodo" / "sessions" / _SESSION_ID
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "session.jsonl").write_text('{"role": "user"}\n', encoding="utf-8")
        port = int(self.args[self.args.index("--port") + 1])
        app = web.Application()
        app.router.add_get("/ws", self.__kodo.handle)
        server = TestServer(app, host="127.0.0.1", port=port)
        await server.start_server()
        self.servers.append(server)
        return _FakeServerProcess(server, None)

    def arg_after(self, flag: str) -> str | None:
        """The value following *flag* on the spawned command line."""
        return self.args[self.args.index(flag) + 1] if flag in self.args else None


# ---------------------------------------------------------------------------
# Fake kodo-llama-server
# ---------------------------------------------------------------------------


class _FakeLlama:
    """``subprocess.Popen``/``subprocess.run`` stand-ins for kodo-llama-server."""

    def __init__(
        self,
        *,
        exit_code: int | None = None,
        ignores_stop: bool = False,
        stop_cli_fails: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.ignores_stop = ignores_stop
        self.stop_cli_fails = stop_cli_fails
        self.spawned: list[list[str]] = []
        self.stop_commands: list[list[str]] = []
        self.killed = False
        self.reaped = False

    def popen(self, args: list[str], **_kwargs: object) -> _FakeLlama.Proc:
        self.spawned.append([str(a) for a in args])
        return _FakeLlama.Proc(self)

    def run(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if self.stop_cli_fails:
            raise OSError("no python")
        self.stop_commands.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, 0, b"", b"")

    class Proc:
        """One fake supervisor process."""

        def __init__(self, owner: _FakeLlama) -> None:
            self.__owner = owner
            self.pid = 777
            self.returncode = owner.exit_code

        def poll(self) -> int | None:
            return self.__owner.exit_code

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and self.__owner.ignores_stop and not self.__owner.killed:
                raise subprocess.TimeoutExpired("kodo.llamaserver", timeout)
            self.__owner.reaped = True
            return 0

        def kill(self) -> None:
            self.__owner.killed = True


class _Health:
    """``urllib.request.urlopen`` stand-in: unreachable for *failures* probes."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.probed: list[str] = []

    def __call__(self, url: str, timeout: float = 0.0) -> _Health:
        self.probed.append(url)
        if len(self.probed) <= self.failures:
            raise urllib.error.URLError("connection refused")
        return self

    status = 200

    def __enter__(self) -> _Health:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's "real" home, empty, under tmp_path; sleeps capped at 10 ms."""
    home = tmp_path / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float, result: object = None) -> object:
        return await real_sleep(min(delay, 0.01), result)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return home


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


@pytest.fixture
async def spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[..., _Spawner]]:
    spawners: list[_Spawner] = []

    def _install(
        kodo: _FakeKodo, *, exit_code: int | None = None, session_log: bool = True
    ) -> _Spawner:
        spawner = _Spawner(kodo, exit_code=exit_code, session_log=session_log)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
        spawners.append(spawner)
        return spawner

    yield _install
    for spawner in spawners:
        for server in spawner.servers:
            await server.close()


@pytest.fixture
def llama(monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple[_FakeLlama, _Health]]:
    def _install(
        health_failures: int = 0,
        *,
        exit_code: int | None = None,
        ignores_stop: bool = False,
        stop_cli_fails: bool = False,
    ) -> tuple[_FakeLlama, _Health]:
        fake = _FakeLlama(
            exit_code=exit_code, ignores_stop=ignores_stop, stop_cli_fails=stop_cli_fails
        )
        health = _Health(health_failures)
        monkeypatch.setattr(subprocess, "Popen", fake.popen)
        monkeypatch.setattr(subprocess, "run", fake.run)
        monkeypatch.setattr(urllib.request, "urlopen", health)
        return fake, health

    return _install


def _events(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]


async def _run(options: HeadlessOptions) -> tuple[RunResult, list[dict[str, object]]]:
    buffer = io.StringIO()
    result = await HeadlessRun(options, EventSink("jsonl", buffer)).run()
    return result, _events(buffer)


# ---------------------------------------------------------------------------
# Cloud model: the full session walk
# ---------------------------------------------------------------------------


async def test_completed_cloud_run_reports_the_turn_and_exports_the_transcript(
    tmp_path: Path, sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    kodo = _FakeKodo()
    spawner = spawn(kodo)
    transcript = tmp_path / "transcript"
    result_path = tmp_path / "out" / "result.json"
    options = HeadlessOptions(
        prompt="fix it",
        model="anthropic/claude-sonnet-5",
        cwd=sandbox,
        thinking_level="high",
        transcript_dir=transcript,
        result_path=result_path,
    )

    result, events = await _run(options)

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    assert result.error is None
    assert (result.session_id, result.model, result.final_phase) == (
        _SESSION_ID,
        "anthropic/claude-sonnet-5",
        "awaiting_user",
    )
    assert result.assistant_text == "all done"
    assert (result.cumulative_input_tokens, result.cumulative_output_tokens) == (12, 5)
    assert result.cumulative_input_tokens_uncached == 10
    assert result.cumulative_usd == 0.25
    assert json.loads(result_path.read_text(encoding="utf-8"))["outcome"] == "completed"
    assert (transcript / "session.jsonl").is_file()
    exported = [e["path"] for e in events if e["type"] == "transcript.exported"]
    assert exported == [str(transcript)]
    # The session walk, in order, and a cloud model has no --llama-url.
    walk = [str(r["type"]) for r in kodo.received]
    assert walk == [
        "hello",
        "top_agents.list",
        "agent.set",
        "workspace.folders",
        "mode.set",
        "thinking_level.set",
        "prompt.submit",
    ]
    folders = kodo.received[3]
    assert folders["physical_root"] == str(sandbox.resolve())
    assert spawner.arg_after("--headless-sandbox") == str(sandbox.resolve())
    assert spawner.arg_after("--llama-url") is None
    # Nothing left behind: the sandbox's .kodo (absent before) and the temp home.
    assert not (sandbox / ".kodo").exists()
    started = events[0]
    assert started["type"] == "run.start"
    assert not Path(str(started["home"])).exists()


async def test_kept_home_and_existing_kodo_dir_survive_the_run(
    tmp_path: Path, sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo())
    (sandbox / ".kodo").mkdir()
    home = tmp_path / "iso"

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox, home=home)
    )

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    assert (sandbox / ".kodo").is_dir()
    assert (home / ".kodo" / "etc" / "settings.json").is_file()


async def test_turn_error_makes_the_run_a_runtime_error(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo(turn="error"))

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox)
    )

    assert result.outcome == RunOutcome.RUNTIME_ERROR.value
    assert result.error == "model exploded"


async def test_turn_that_never_rests_times_out_and_is_stopped(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    kodo = _FakeKodo(turn="hang")
    spawn(kodo)

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox, timeout=0.05)
    )

    assert result.outcome == RunOutcome.TIMEOUT.value
    assert result.error is not None and "did not finish" in result.error
    assert result.final_phase == "stopped"
    assert "stop" in [r["type"] for r in kodo.received]


async def test_stop_request_ends_the_turn_as_stopped(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    kodo = _FakeKodo(turn="hang")
    spawn(kodo)
    options = HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox)
    buffer = io.StringIO()
    run = HeadlessRun(options, EventSink("jsonl", buffer))
    run.request_stop()

    result = await run.run()

    assert result.outcome == RunOutcome.STOPPED.value
    assert result.error == "Stopped by a signal before the turn finished"
    assert "stop" in [r["type"] for r in kodo.received]


async def test_connection_dropped_mid_turn_is_a_crash(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo(turn="drop"))

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox)
    )

    assert result.outcome == RunOutcome.CRASHED.value
    assert result.error is not None and "closed" in result.error


# ---------------------------------------------------------------------------
# Setup failures
# ---------------------------------------------------------------------------


async def test_unknown_top_level_agent_is_a_startup_error(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    kodo = _FakeKodo(agents=("kodo_other", "kodo_problem_solver_v2"))
    spawn(kodo)

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox)
    )

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error is not None
    assert "Unknown top-level agent 'kodo_problem_solver'" in result.error
    assert "kodo_other, kodo_problem_solver_v2" in result.error
    assert "prompt.submit" not in [r["type"] for r in kodo.received]


async def test_invalid_thinking_level_is_a_startup_error(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo(thinking_ok=False))

    result, _ = await _run(
        HeadlessOptions(
            prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox, thinking_level="ultra"
        )
    )

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error is not None and "'ultra' is not valid" in result.error


async def test_refused_setup_request_is_a_startup_error(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo(failing_request="agent.set"))

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox)
    )

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error is not None and "agent.set failed" in result.error


async def test_server_exiting_before_it_listens_is_a_startup_error(
    sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo(), exit_code=2)

    result, _ = await _run(
        HeadlessOptions(prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox)
    )

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error is not None and "exited with code 2" in result.error
    assert result.session_id == ""


async def test_unexpected_failure_is_reported_as_a_crash(tmp_path: Path, sandbox: Path) -> None:
    options = HeadlessOptions(
        prompt="go",
        model="anthropic/claude-sonnet-5",
        cwd=sandbox,
        registry_file=tmp_path / "missing-registry.json",
    )

    result, events = await _run(options)

    assert result.outcome == RunOutcome.CRASHED.value
    assert result.error is not None and result.error.startswith("FileNotFoundError: ")
    assert events[-1]["type"] == "run.result"


async def test_malformed_model_is_a_startup_error(sandbox: Path) -> None:
    result, _ = await _run(HeadlessOptions(prompt="go", model="anthropic/", cwd=sandbox))

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error is not None and result.error.startswith("Invalid --model 'anthropic/'")


# ---------------------------------------------------------------------------
# Transcript export
# ---------------------------------------------------------------------------


async def test_missing_session_log_is_a_warning_not_a_failure(
    tmp_path: Path, sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo(), session_log=False)
    target = tmp_path / "transcript"

    result, events = await _run(
        HeadlessOptions(
            prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox, transcript_dir=target
        )
    )

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    warnings = [str(e["message"]) for e in events if e["type"] == "warning"]
    assert any("No session log to export" in w for w in warnings)
    assert not target.exists()


async def test_unwritable_transcript_target_is_a_warning_not_a_failure(
    tmp_path: Path, sandbox: Path, spawn: Callable[..., _Spawner]
) -> None:
    spawn(_FakeKodo())
    target = tmp_path / "transcript"
    target.write_text("a file, not a directory", encoding="utf-8")

    result, events = await _run(
        HeadlessOptions(
            prompt="go", model="anthropic/claude-sonnet-5", cwd=sandbox, transcript_dir=target
        )
    )

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    warnings = [str(e["message"]) for e in events if e["type"] == "warning"]
    assert any("Could not export the session log" in w for w in warnings)


# ---------------------------------------------------------------------------
# Local model: attached or spawned llama-server
# ---------------------------------------------------------------------------


async def test_local_model_attaches_to_the_given_llama_url(
    sandbox: Path,
    spawn: Callable[..., _Spawner],
    llama: Callable[..., tuple[_FakeLlama, _Health]],
) -> None:
    spawner = spawn(_FakeKodo())
    fake_llama, _ = llama()

    result, events = await _run(
        HeadlessOptions(prompt="go", model="m-q4", cwd=sandbox, llama_url="http://host:9000")
    )

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    assert spawner.arg_after("--llama-url") == "http://host:9000"
    assert fake_llama.spawned == []
    assert "llama.spawn" not in [e["type"] for e in events]


async def test_spawned_llama_server_is_awaited_used_and_stopped(
    isolated_home: Path,
    sandbox: Path,
    spawn: Callable[..., _Spawner],
    llama: Callable[..., tuple[_FakeLlama, _Health]],
) -> None:
    spawner = spawn(_FakeKodo())
    fake_llama, health = llama(health_failures=1)

    result, events = await _run(
        HeadlessOptions(prompt="go", model="m-q4", cwd=sandbox, llama_port=8123)
    )

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    command = fake_llama.spawned[0]
    assert command[command.index("--model") + 1] == "m-q4"
    assert command[command.index("--port") + 1] == "8123"
    assert "--foreground" in command
    assert (isolated_home / ".kodo" / "logs" / "llama-server-8123-headless.log").is_file()
    assert health.probed[-1] == "http://127.0.0.1:8123/health"
    assert len(health.probed) >= 2  # the first probe was refused, the run kept waiting
    assert spawner.arg_after("--llama-url") == "http://127.0.0.1:8123"
    kinds = [e["type"] for e in events]
    assert kinds.index("llama.spawn") < kinds.index("llama.ready")
    stop = fake_llama.stop_commands[0]
    assert stop[stop.index("stop") + 1 :] == ["--port", "8123"]
    assert fake_llama.reaped and not fake_llama.killed


async def test_llama_server_ignoring_stop_is_killed(
    sandbox: Path,
    spawn: Callable[..., _Spawner],
    llama: Callable[..., tuple[_FakeLlama, _Health]],
) -> None:
    spawn(_FakeKodo())
    fake_llama, _ = llama(ignores_stop=True, stop_cli_fails=True)

    result, _ = await _run(HeadlessOptions(prompt="go", model="m-q4", cwd=sandbox))

    assert result.outcome == RunOutcome.COMPLETED.value, result.error
    assert fake_llama.killed and fake_llama.reaped


async def test_llama_server_exiting_early_is_a_startup_error(
    sandbox: Path,
    spawn: Callable[..., _Spawner],
    llama: Callable[..., tuple[_FakeLlama, _Health]],
) -> None:
    spawner = spawn(_FakeKodo())
    llama(exit_code=3)

    result, _ = await _run(HeadlessOptions(prompt="go", model="m-q4", cwd=sandbox, llama_port=8124))

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error is not None
    assert "exited with code 3" in result.error
    assert "llama-server-8124-headless.log" in result.error
    assert spawner.args == []  # kodo-server was never spawned


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


def _local_run(sandbox: Path) -> HeadlessRun:
    return HeadlessRun(
        HeadlessOptions(prompt="go", model="m-q4", cwd=sandbox), EventSink("jsonl", io.StringIO())
    )


async def _stopped_while_llama_starts(run: HeadlessRun) -> None:
    result = await run.run()
    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert result.error == "Stopped while llama-server was starting"


async def test_signal_handlers_route_sigint_and_sigterm_to_request_stop(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    llama: Callable[..., tuple[_FakeLlama, _Health]],
) -> None:
    llama(health_failures=10_000)
    installed: dict[int, Callable[[], object]] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda signum, callback: installed.update({signum: callback})
    )
    run = _local_run(sandbox)

    install_signal_handlers(run)

    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    installed[signal.SIGTERM]()
    await _stopped_while_llama_starts(run)


async def test_signal_handlers_fall_back_to_signal_signal(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    llama: Callable[..., tuple[_FakeLlama, _Health]],
) -> None:
    llama(health_failures=10_000)
    loop = asyncio.get_running_loop()

    def no_loop_handlers(_signum: int, _callback: Callable[[], object]) -> None:
        raise NotImplementedError

    installed: dict[int, Callable[[int, object], object]] = {}
    monkeypatch.setattr(loop, "add_signal_handler", no_loop_handlers)
    monkeypatch.setattr(signal, "signal", lambda signum, h: installed.update({signum: h}))
    run = _local_run(sandbox)

    install_signal_handlers(run)

    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    installed[signal.SIGINT](signal.SIGINT, None)
    await asyncio.sleep(0)  # the handler hands request_stop to the loop thread-safely
    await _stopped_while_llama_starts(run)

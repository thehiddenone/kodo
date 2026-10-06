"""Lifecycle tests for the titler's dedicated llama-server, driven through ``kodo.titling``.

Everything below the public API is faked at its stdlib/third-party seam, so no
real process, socket, model download or ``os.kill`` is ever touched:

* ``asyncio.create_subprocess_exec`` spawns an entry in an in-memory process
  table instead of a real ``llama-server``;
* ``os.kill`` consults and mutates that table (signal 0 probes it, SIGTERM /
  SIGKILL end an entry) and never reaches the real OS;
* ``aiohttp.ClientSession`` answers the ``/health`` poll from a script;
* ``asyncio.sleep`` yields without waiting, so grace periods and the 60-second
  health timeout run instantly;
* :class:`kodo.llms.local.LocalModelManager`'s model lookup/download are
  backed by an in-memory "downloaded" set;
* ``openai.AsyncOpenAI`` returns canned completions.

``HOME`` is redirected to ``tmp_path`` so the llama.cpp install record and the
titler's runtime file live in a throwaway ``~/.kodo``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import aiohttp
import openai
import pytest

from kodo.llms.local import LocalModelManager
from kodo.titling import (
    DEFAULT_HOUSEKEEPER_LLM_ID,
    HOUSEKEEPER_LLM_OPTIONS,
    generate_greeting,
    generate_project_name,
    generate_title,
    start_titling,
    stop_titling,
    titler_home_dir,
)

_LOGGER = "kodo.titling"
_OTHER_MODEL_ID = next(k for k in HOUSEKEEPER_LLM_OPTIONS if k != DEFAULT_HOUSEKEEPER_LLM_ID)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Processes:
    """An in-memory process table standing in for ``os.kill`` and process spawning."""

    def __init__(self) -> None:
        self.alive: set[int] = set()
        self.ignores_sigterm: set[int] = set()
        self.commands: list[list[str]] = []
        self.sigterm_error: Exception | None = None
        self.crash_on_spawn = False
        self.spawn_output = b""
        self.on_spawn: Callable[[Path], None] | None = None
        self.__next_pid = 3_900_001

    def new_pid(self) -> int:
        pid = self.__next_pid
        self.__next_pid += 1
        return pid

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.alive:
            raise ProcessLookupError(3, "No such process")
        if sig == 0:
            return
        if sig == signal.SIGTERM:
            if self.sigterm_error is not None:
                raise self.sigterm_error
            if pid in self.ignores_sigterm:
                return
        self.alive.discard(pid)

    async def spawn(self, *cmd: str, stdout: IO[bytes], **kwargs: object) -> object:
        self.commands.append(list(cmd))
        stdout.write(self.spawn_output)
        stdout.flush()
        pid = self.new_pid()
        if not self.crash_on_spawn:
            self.alive.add(pid)
        if self.on_spawn is not None:
            self.on_spawn(Path(stdout.name))
        return _Proc(pid)


@dataclass
class _Proc:
    pid: int


class _Health:
    """Scripted ``/health`` replies: one per poll, the last one repeating."""

    def __init__(self) -> None:
        self.script: list[int | Exception] = [200]
        self.polls = 0

    def session(self) -> _HealthSession:
        return _HealthSession(self)

    def next_reply(self) -> int | Exception:
        reply = self.script[min(self.polls, len(self.script) - 1)]
        self.polls += 1
        return reply


class _HealthSession:
    def __init__(self, health: _Health) -> None:
        self.__health = health

    async def __aenter__(self) -> _HealthSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _HealthResponse:
        return _HealthResponse(self.__health.next_reply())


class _HealthResponse:
    def __init__(self, reply: int | Exception) -> None:
        self.__reply = reply

    async def __aenter__(self) -> _HealthResponse:
        if isinstance(self.__reply, Exception):
            raise self.__reply
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def status(self) -> int:
        assert isinstance(self.__reply, int)
        return self.__reply


@dataclass
class _Models:
    """Which housekeeper models count as downloaded, plus a scripted downloader."""

    root: Path
    ready: set[str] = field(default_factory=set)
    downloads: list[str] = field(default_factory=list)
    download_lands = True
    download_gate: asyncio.Event | None = None
    download_error: Exception | None = None

    def path_for(self, model_id: str) -> Path | None:
        return self.root / f"{model_id}.gguf" if model_id in self.ready else None

    async def download(self, model_id: str) -> None:
        self.downloads.append(model_id)
        if self.download_gate is not None:
            await self.download_gate.wait()
        if self.download_error is not None:
            raise self.download_error
        if self.download_lands:
            self.ready.add(model_id)


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Message(content)


class _Completion:
    def __init__(self, content: str | None) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, replies: list[str | None]) -> None:
        self.__replies = replies

    async def create(self, **kwargs: Any) -> _Completion:
        return _Completion(self.__replies.pop(0) if len(self.__replies) > 1 else self.__replies[0])


class _Chat:
    def __init__(self, replies: list[str | None]) -> None:
        self.completions = _Completions(replies)


class _Client:
    def __init__(self, replies: list[str | None]) -> None:
        self.chat = _Chat(replies)


@dataclass
class _Env:
    kodo_dir: Path
    processes: _Processes
    health: _Health
    models: _Models
    replies: list[str | None]


def _runtime_file() -> Path:
    return titler_home_dir() / "llama-server.json"


def _write_runtime_file(payload: object) -> None:
    path = _runtime_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _runtime_pid() -> int:
    return int(json.loads(_runtime_file().read_text(encoding="utf-8"))["pid"])


async def _drain_background_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Env]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    kodo_dir = home / ".kodo"

    # A llama.cpp install record pointing at an (inert) executable file.
    executable = kodo_dir / "llama.cpp" / "b1" / "llama-server"
    executable.parent.mkdir(parents=True)
    executable.write_text("not a real binary", encoding="utf-8")
    (kodo_dir / "llama.cpp" / "llama-meta.json").write_text(
        json.dumps({"build": 1, "executable": str(executable)}), encoding="utf-8"
    )

    processes = _Processes()
    health = _Health()
    models = _Models(root=tmp_path / "models", ready={DEFAULT_HOUSEKEEPER_LLM_ID})
    replies: list[str | None] = ["Draft Title", "Refined Title"]

    real_sleep = asyncio.sleep

    async def _instant_sleep(delay: float, result: object = None) -> object:
        return await real_sleep(0, result)

    async def _download(
        self: LocalModelManager, model_id: str, *args: object, **kw: object
    ) -> None:
        await models.download(model_id)

    monkeypatch.setattr(os, "kill", processes.kill)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", processes.spawn)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(aiohttp, "ClientSession", health.session)
    monkeypatch.setattr(
        LocalModelManager, "get_model_path", lambda self, model_id: models.path_for(model_id)
    )
    monkeypatch.setattr(LocalModelManager, "download_model", _download)
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: _Client(replies))

    yield _Env(kodo_dir, processes, health, models, replies)

    # Fakes are still installed here (monkeypatch tears down after this
    # fixture), so this never signals a real pid.
    models.download_gate = None
    await _drain_background_tasks()
    await stop_titling()


async def _start(env: _Env, model_id: str = DEFAULT_HOUSEKEEPER_LLM_ID) -> None:
    await start_titling(env.kodo_dir, model_id)


async def _titling_works() -> bool:
    return await generate_title("please add a csv export endpoint to the reports page") is not None


# ---------------------------------------------------------------------------
# Happy path + the generate_* capabilities
# ---------------------------------------------------------------------------


async def test_started_titler_serves_titles_and_records_its_runtime(env: _Env) -> None:
    await _start(env)

    assert await generate_title("please add a csv export") == "Refined Title"
    runtime = json.loads(_runtime_file().read_text(encoding="utf-8"))
    assert runtime["model_id"] == DEFAULT_HOUSEKEEPER_LLM_ID
    assert runtime["pid"] in env.processes.alive


async def test_project_name_with_empty_completion_is_none(env: _Env) -> None:
    await _start(env)
    env.replies[:] = [""]
    assert await generate_project_name("a todo app") is None


async def test_greeting_with_empty_completion_is_none(env: _Env) -> None:
    await _start(env)
    env.replies[:] = [None]
    assert await generate_greeting() is None


async def test_greeting_and_project_name_ride_the_started_server(env: _Env) -> None:
    await _start(env)
    env.replies[:] = ["<think>hmm</think> Hello from Kodo"]
    assert await generate_greeting() == "Hello from Kodo"
    assert await generate_project_name("a todo app") == "Hello from Kodo"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def test_health_check_tolerates_errors_and_non_200_until_ready(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.health.script = [503, *([aiohttp.ClientConnectionError("refused")] * 12), 200]

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _start(env)

    assert await _titling_works()
    assert "status=503" in caplog.text
    assert "health check request failed" in caplog.text


async def test_health_check_timeout_leaves_titling_unavailable(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.health.script = [aiohttp.ClientConnectionError("refused")]

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _start(env)

    assert not await _titling_works()
    assert "did not become ready" in caplog.text
    assert not _runtime_file().exists()


async def test_crash_before_ready_reports_the_tail_of_the_startup_output(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.processes.crash_on_spawn = True
    env.processes.spawn_output = b"HEAD-MARKER" + b"x" * 5000 + b"TAIL-MARKER"

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _start(env)

    assert not await _titling_works()
    assert "exited before becoming ready" in caplog.text
    assert "TAIL-MARKER" in caplog.text
    assert "HEAD-MARKER" not in caplog.text


async def test_crash_with_an_unreadable_startup_log_still_reports_the_crash(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    def _replace_log_with_directory(log: Path) -> None:
        log.unlink()
        log.mkdir()

    env.processes.crash_on_spawn = True
    env.processes.spawn_output = b"never readable"
    env.processes.on_spawn = _replace_log_with_directory

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _start(env)

    assert not await _titling_works()
    assert "exited before becoming ready" in caplog.text
    assert "never readable" not in caplog.text


# ---------------------------------------------------------------------------
# Runtime file from a previous kodo run
# ---------------------------------------------------------------------------


async def test_unparseable_runtime_file_is_replaced_by_a_fresh_server(env: _Env) -> None:
    _write_runtime_file("{definitely not json")

    await _start(env)

    assert await _titling_works()
    assert len(env.processes.commands) == 1
    assert _runtime_pid() in env.processes.alive


async def test_runtime_file_for_a_dead_process_is_not_adopted(env: _Env) -> None:
    dead_pid = env.processes.new_pid()
    _write_runtime_file({"pid": dead_pid, "port": 8043, "model_id": DEFAULT_HOUSEKEEPER_LLM_ID})

    await _start(env)

    assert await _titling_works()
    assert len(env.processes.commands) == 1
    assert _runtime_pid() != dead_pid


async def test_orphan_running_another_model_that_ignores_sigterm_is_killed(env: _Env) -> None:
    orphan = env.processes.new_pid()
    env.processes.alive.add(orphan)
    env.processes.ignores_sigterm.add(orphan)
    _write_runtime_file({"pid": orphan, "port": 8043, "model_id": _OTHER_MODEL_ID})

    await _start(env)

    assert orphan not in env.processes.alive
    assert await _titling_works()
    assert _runtime_pid() != orphan


# ---------------------------------------------------------------------------
# Restart / stop
# ---------------------------------------------------------------------------


async def test_start_titling_respawns_a_server_that_died(env: _Env) -> None:
    await _start(env)
    env.processes.alive.clear()
    assert not await _titling_works()

    await _start(env)

    assert len(env.processes.commands) == 2
    assert await _titling_works()


async def test_stop_titling_after_the_server_died_clears_it(env: _Env) -> None:
    await _start(env)
    env.processes.alive.clear()

    await stop_titling()

    assert not await _titling_works()


async def test_stop_titling_kills_a_server_that_ignores_sigterm(env: _Env) -> None:
    await _start(env)
    pid = _runtime_pid()
    env.processes.ignores_sigterm.add(pid)

    await stop_titling()

    assert pid not in env.processes.alive
    assert not _runtime_file().exists()
    assert not await _titling_works()


async def test_stop_titling_swallows_a_stop_failure(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    await _start(env)
    env.processes.sigterm_error = RuntimeError("signal delivery exploded")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await stop_titling()

    assert "failed to stop titler llama-server" in caplog.text
    assert not await _titling_works()


# ---------------------------------------------------------------------------
# Model availability
# ---------------------------------------------------------------------------


async def test_download_that_never_lands_leaves_titling_unavailable(env: _Env) -> None:
    env.models.ready.clear()
    env.models.download_lands = False

    await _start(env)

    assert env.models.downloads == [DEFAULT_HOUSEKEEPER_LLM_ID]
    assert env.processes.commands == []
    assert not await _titling_works()


async def test_background_download_for_a_pending_switch_is_not_duplicated(env: _Env) -> None:
    await _start(env)
    gate = asyncio.Event()
    env.models.download_gate = gate

    await _start(env, _OTHER_MODEL_ID)
    await _start(env, _OTHER_MODEL_ID)
    gate.set()
    await _drain_background_tasks()

    assert env.models.downloads == [_OTHER_MODEL_ID]
    # The original model kept serving the whole time.
    assert len(env.processes.commands) == 1
    assert await _titling_works()

    # Once landed, explicitly re-selecting the model swaps the server over.
    await _start(env, _OTHER_MODEL_ID)
    assert len(env.processes.commands) == 2
    assert json.loads(_runtime_file().read_text(encoding="utf-8"))["model_id"] == _OTHER_MODEL_ID


async def test_failed_background_download_is_logged_and_retried_on_the_next_call(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    await _start(env)
    env.models.download_error = OSError("connection reset")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _start(env, _OTHER_MODEL_ID)
        await _drain_background_tasks()

    assert "failed to download" in caplog.text
    assert await _titling_works()

    # The failure did not leave the model marked as "already downloading".
    env.models.download_error = None
    await _start(env, _OTHER_MODEL_ID)
    await _drain_background_tasks()
    assert env.models.downloads == [_OTHER_MODEL_ID, _OTHER_MODEL_ID]
    assert _OTHER_MODEL_ID in env.models.ready

"""Behavioral tests for :class:`kodo.llms.llamacpp.LlamaServer`'s lifecycle and PID helpers.

No real ``llama-server`` (or any child process) is launched and no real
signal is sent: ``asyncio.create_subprocess_exec`` and ``os.kill`` are
monkeypatched onto a small fake process table, and the health endpoint the
server polls is an :mod:`aiohttp` app on loopback. The Windows branches of
the PID helpers are driven by monkeypatching ``sys.platform`` and a fake
``ctypes.windll.kernel32``.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast

import pytest
from aiohttp import web

from kodo.llms.llamacpp import (
    LlamaServer,
    LlamaServerConfig,
    RunningServer,
    find_running_server,
    is_pid_alive,
    kill_pid,
    terminate_pid,
)

_PID = 424242


# ---------------------------------------------------------------------------
# Fake process layer
# ---------------------------------------------------------------------------


@dataclass
class _Procs:
    """A fake OS process table behind monkeypatched ``os.kill``/``create_subprocess_exec``."""

    alive: set[int] = field(default_factory=set)
    ignores_sigterm: bool = False
    exits_immediately: bool = False
    output: bytes = b""
    delete_startup_log: bool = False
    signals: list[tuple[int, int]] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if sig == 0:
            return
        self.signals.append((pid, sig))
        if sig == signal.SIGTERM and self.ignores_sigterm:
            return
        self.alive.discard(pid)

    async def create_subprocess_exec(
        self, *argv: str, stdout: BinaryIO, stderr: int
    ) -> SimpleNamespace:
        self.argv = list(argv)
        stdout.write(self.output)
        stdout.flush()
        if self.delete_startup_log:
            Path(stdout.name).unlink()
        if not self.exits_immediately:
            self.alive.add(_PID)
        return SimpleNamespace(pid=_PID)


@pytest.fixture
def procs(monkeypatch: pytest.MonkeyPatch) -> _Procs:
    table = _Procs()
    monkeypatch.setattr(os, "kill", table.kill)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", table.create_subprocess_exec)
    return table


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the module's poll/grace sleeps so timeouts resolve instantly."""
    real_sleep = asyncio.sleep

    async def no_wait(delay: float, result: object = None) -> object:
        await real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", no_wait)


@dataclass
class _Health:
    port: int
    status: int = 200


@pytest.fixture
async def health() -> AsyncIterator[_Health]:
    state = _Health(port=0)

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=state.status)

    app = web.Application()
    app.router.add_get("/health", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.port = cast(tuple[str, int], runner.addresses[0])[1]
    try:
        yield state
    finally:
        await runner.cleanup()


def _config(tmp_path: Path, port: int, **overrides: object) -> LlamaServerConfig:
    base: dict[str, object] = {
        "executable": tmp_path / "llama-server",
        "model_path": tmp_path / "model.gguf",
        "kodo_dir": tmp_path / "kodo",
        "model_name": "fake-model",
        "port": port,
    }
    base.update(overrides)
    return LlamaServerConfig(**base)  # type: ignore[arg-type]


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


async def test_start_launches_with_profile_flags_and_records_runtime_file(
    tmp_path: Path, procs: _Procs, health: _Health
) -> None:
    config = _config(tmp_path, health.port, alias="served-name")
    server = LlamaServer(config, {"--ctx-size": "4096", "--jinja": ""})

    await server.start()

    assert server.is_running
    assert server.pid == _PID
    assert server.port == health.port
    assert server.base_url == f"http://127.0.0.1:{health.port}"
    assert server.model_name == "fake-model"
    assert LlamaServer.get_active_llama_server() is server

    argv = procs.argv
    assert argv[0] == str(config.executable)
    assert _flag_value(argv, "--model") == str(config.model_path)
    assert _flag_value(argv, "--alias") == "served-name"
    assert _flag_value(argv, "--ctx-size") == "4096"
    assert argv[-1] == "--jinja"  # a bare flag gets no value
    assert _flag_value(argv, "--log-file") == str(tmp_path / "kodo" / "logs" / "llama-server.log")

    assert find_running_server(tmp_path / "kodo") == RunningServer(
        pid=_PID, host="127.0.0.1", port=health.port, model="fake-model"
    )


async def test_start_without_alias_passes_no_alias_flag(
    tmp_path: Path, procs: _Procs, health: _Health
) -> None:
    await LlamaServer(_config(tmp_path, health.port)).start()

    assert "--alias" not in procs.argv


async def test_start_refuses_while_already_running(
    tmp_path: Path, procs: _Procs, health: _Health
) -> None:
    server = LlamaServer(_config(tmp_path, health.port))
    await server.start()

    with pytest.raises(RuntimeError, match="already running"):
        await server.start()


async def test_custom_runtime_and_log_files_keep_the_server_unadoptable(
    tmp_path: Path, procs: _Procs, health: _Health
) -> None:
    runtime_file = tmp_path / "standalone" / "server.json"
    log_file = tmp_path / "standalone" / "serve.log"
    server = LlamaServer(
        _config(tmp_path, health.port, runtime_file=runtime_file, log_file=log_file)
    )

    await server.start()

    assert _flag_value(procs.argv, "--log-file") == str(log_file)
    assert (tmp_path / "standalone" / "serve-startup.log").is_file()
    assert json.loads(runtime_file.read_text(encoding="utf-8"))["pid"] == _PID
    assert find_running_server(tmp_path / "kodo") is None

    await server.stop()
    assert not runtime_file.exists()


async def test_stop_terminates_gracefully_and_clears_runtime_file(
    tmp_path: Path, procs: _Procs, health: _Health
) -> None:
    server = LlamaServer(_config(tmp_path, health.port))
    await server.start()

    await server.stop()

    assert not server.is_running
    assert server.pid is None
    assert _PID not in procs.alive
    assert procs.signals == [(_PID, signal.SIGTERM)]
    assert find_running_server(tmp_path / "kodo") is None


async def test_stop_kills_a_process_that_ignores_sigterm(
    tmp_path: Path, procs: _Procs, health: _Health, fast_sleep: None
) -> None:
    server = LlamaServer(_config(tmp_path, health.port))
    await server.start()
    procs.ignores_sigterm = True

    await server.stop()

    assert _PID not in procs.alive
    assert procs.signals[-1] == (_PID, signal.SIGKILL)
    assert server.pid is None


async def test_stop_is_a_noop_when_never_started(tmp_path: Path, procs: _Procs) -> None:
    server = LlamaServer(_config(tmp_path, 1))

    await server.stop()

    assert server.pid is None
    assert procs.signals == []


async def test_stop_forgets_a_process_that_already_exited(
    tmp_path: Path, procs: _Procs, health: _Health
) -> None:
    server = LlamaServer(_config(tmp_path, health.port))
    await server.start()
    procs.alive.clear()  # died on its own

    await server.stop()

    assert server.pid is None
    assert procs.signals == []


async def test_start_times_out_when_health_never_turns_ready(
    tmp_path: Path, procs: _Procs, health: _Health, fast_sleep: None
) -> None:
    health.status = 503
    server = LlamaServer(_config(tmp_path, health.port))

    with pytest.raises(TimeoutError, match="did not become ready"):
        await server.start()


# ---------------------------------------------------------------------------
# Crash-before-ready diagnostics
# ---------------------------------------------------------------------------


async def test_crash_message_keeps_only_the_tail_of_long_output(
    tmp_path: Path, procs: _Procs
) -> None:
    procs.exits_immediately = True
    procs.output = b"HEAD-MARKER " + b"x" * 5000 + b" TAIL-MARKER"
    server = LlamaServer(_config(tmp_path, 1))

    with pytest.raises(RuntimeError) as exc_info:
        await server.start()

    message = str(exc_info.value)
    assert "exited before becoming ready" in message
    assert "TAIL-MARKER" in message
    assert "HEAD-MARKER" not in message


async def test_crash_message_written_next_to_a_custom_log_file(
    tmp_path: Path, procs: _Procs
) -> None:
    procs.exits_immediately = True
    procs.output = b"error: unknown flag"
    log_file = tmp_path / "logs" / "custom.log"
    server = LlamaServer(_config(tmp_path, 1, log_file=log_file))

    with pytest.raises(RuntimeError, match="unknown flag"):
        await server.start()
    assert (tmp_path / "logs" / "custom-startup.log").read_text() == "error: unknown flag"


async def test_crash_message_survives_an_unreadable_startup_log(
    tmp_path: Path, procs: _Procs
) -> None:
    procs.exits_immediately = True
    procs.output = b"lost output"
    procs.delete_startup_log = True
    server = LlamaServer(_config(tmp_path, 1))

    with pytest.raises(RuntimeError) as exc_info:
        await server.start()

    message = str(exc_info.value)
    assert "exited before becoming ready" in message
    assert "Output from llama-server" not in message


# ---------------------------------------------------------------------------
# adopt / find_running_server
# ---------------------------------------------------------------------------


def test_adopt_takes_over_a_surviving_process(tmp_path: Path, procs: _Procs) -> None:
    procs.alive.add(_PID)
    server = LlamaServer(_config(tmp_path, 8042))

    server.adopt(RunningServer(pid=_PID, host="10.0.0.5", port=9100, model="m"))

    assert server.is_running
    assert server.pid == _PID
    assert server.base_url == "http://10.0.0.5:9100"
    with pytest.raises(RuntimeError, match="already running"):
        server.adopt(RunningServer(pid=_PID, host="10.0.0.5", port=9100, model="m"))


def _write_runtime(kodo_dir: Path, text: str) -> Path:
    path = kodo_dir / "llama.cpp" / "llama-server.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_find_running_server_without_runtime_file(tmp_path: Path, procs: _Procs) -> None:
    assert find_running_server(tmp_path) is None


def test_find_running_server_detects_a_live_process_with_defaults(
    tmp_path: Path, procs: _Procs
) -> None:
    procs.alive.add(_PID)
    _write_runtime(tmp_path, json.dumps({"pid": _PID, "port": 9000}))

    assert find_running_server(tmp_path) == RunningServer(
        pid=_PID, host="127.0.0.1", port=9000, model=""
    )


@pytest.mark.parametrize(
    "text",
    ["{broken", json.dumps({"port": 9000}), json.dumps({"pid": _PID, "port": 9000})],
    ids=["corrupt", "missing-pid", "stale-pid"],
)
def test_find_running_server_removes_unusable_runtime_files(
    tmp_path: Path, procs: _Procs, text: str
) -> None:
    path = _write_runtime(tmp_path, text)

    assert find_running_server(tmp_path) is None
    assert not path.exists()


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------


def test_signal_helpers_tolerate_a_vanished_process(procs: _Procs) -> None:
    assert is_pid_alive(_PID) is False
    terminate_pid(_PID)
    kill_pid(_PID)
    assert procs.signals == []


def test_signal_helpers_end_a_live_process(procs: _Procs) -> None:
    procs.alive.update({1, 2})

    terminate_pid(1)
    kill_pid(2)

    assert procs.alive == set()
    assert procs.signals == [(1, signal.SIGTERM), (2, signal.SIGKILL)]


@dataclass
class _Kernel32:
    """Fake ``kernel32`` exposing just the calls the PID helpers make."""

    handle: int = 7
    exit_code_ok: bool = True
    exit_code: int = 259  # STILL_ACTIVE
    open_handles: set[int] = field(default_factory=set)
    terminated: list[int] = field(default_factory=list)

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802
        if self.handle:
            self.open_handles.add(self.handle)
        return self.handle

    def GetExitCodeProcess(self, handle: int, ref: object) -> int:  # noqa: N802
        if not self.exit_code_ok:
            return 0
        cast(ctypes.c_ulong, cast(SimpleNamespace, ref)._obj).value = self.exit_code
        return 1

    def TerminateProcess(self, handle: int, code: int) -> int:  # noqa: N802
        self.terminated.append(handle)
        return 1

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        self.open_handles.discard(handle)
        return 1


@pytest.fixture
def kernel32(monkeypatch: pytest.MonkeyPatch) -> Callable[[], _Kernel32]:
    """Pretend to be on Windows, with *kernel32* backing ``ctypes.windll``.

    ``os.kill`` is replaced with a tripwire: on Windows ``os.kill(pid, 0)`` is
    a real Ctrl+C, so the helpers must never reach it there.
    """

    def tripwire(pid: int, sig: int) -> None:
        raise AssertionError("os.kill must not be used on Windows")

    def install() -> _Kernel32:
        fake = _Kernel32()
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=fake), raising=False)
        monkeypatch.setattr(os, "kill", tripwire)
        monkeypatch.setattr(sys, "platform", "win32")
        return fake

    return install


def test_windows_liveness_reports_a_still_active_process(
    kernel32: Callable[[], _Kernel32],
) -> None:
    fake = kernel32()

    alive = is_pid_alive(_PID)

    assert alive is True
    assert fake.open_handles == set()


@pytest.mark.parametrize(
    ("handle", "exit_code_ok", "exit_code"),
    [(0, True, 259), (7, False, 259), (7, True, 0)],
    ids=["cannot-open", "exit-code-unavailable", "exited"],
)
def test_windows_liveness_reports_dead_processes(
    kernel32: Callable[[], _Kernel32], handle: int, exit_code_ok: bool, exit_code: int
) -> None:
    fake = kernel32()
    fake.handle = handle
    fake.exit_code_ok = exit_code_ok
    fake.exit_code = exit_code

    alive = is_pid_alive(_PID)

    assert alive is False
    assert fake.open_handles == set()


def test_windows_kill_terminates_through_a_process_handle(
    kernel32: Callable[[], _Kernel32],
) -> None:
    fake = kernel32()

    kill_pid(_PID)

    assert fake.terminated == [7]
    assert fake.open_handles == set()


def test_windows_kill_of_a_vanished_process_is_not_an_error(
    kernel32: Callable[[], _Kernel32],
) -> None:
    fake = kernel32()
    fake.handle = 0

    kill_pid(_PID)

    assert fake.terminated == []

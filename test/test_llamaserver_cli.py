"""In-process tests of the ``kodo-llama-server`` CLI (:func:`kodo.llamaserver.main`).

No process is ever spawned and nothing is signalled: the process table
(``is_pid_alive`` / ``terminate_pid`` / ``kill_pid``), ``subprocess.Popen``,
the ``/health`` probe, the port probe and the clock are all fakes, and
``LlamaServer`` is replaced by an in-memory stand-in for the foreground
supervisor. The kodo home is a throwaway ``HOME`` under ``tmp_path`` holding
a ``custom_file`` registry entry, so a model counts as installed without any
download state.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from kodo import __version__
from kodo.llamaserver import StandaloneState, Supervisor, main
from kodo.llms.llamacpp import LlamaServerConfig

_MODEL = "fake-model"
_OTHER_MODEL = "other-model"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Processes:
    """A fake OS process table standing in for kodo.llms.llamacpp's pid helpers."""

    def __init__(self) -> None:
        self.alive: set[int] = set()
        self.stubborn: set[int] = set()
        self.linked: dict[int, int] = {}
        self.terminated: list[int] = []
        self.killed: list[int] = []

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        if pid not in self.stubborn:
            self.__end(pid)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        self.__end(pid)

    def __end(self, pid: int) -> None:
        self.alive.discard(pid)
        follower = self.linked.get(pid)
        if follower is not None:
            self.alive.discard(follower)


class _Clock:
    """Fake monotonic clock; ``sleep`` advances it instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.on_sleep: Callable[[], None] | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


class _FakePopen:
    """Stands in for the detached supervisor process."""

    instances: list[_FakePopen] = []
    exit_code: int | None = None
    output: bytes = b""
    delete_log: bool = False
    ready_after_polls: int | None = None
    kodo_dir: Path = Path()

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4242
        self.__polls = 0
        stdout = kwargs["stdout"]
        assert isinstance(stdout, io.BufferedWriter)
        stdout.write(self.output)
        stdout.flush()
        if self.delete_log:
            Path(stdout.name).unlink()
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        self.__polls += 1
        ready = self.ready_after_polls
        if ready is not None and self.__polls >= ready:
            port = int(self.cmd[self.cmd.index("--port") + 1])
            StandaloneState(
                supervisor_pid=self.pid,
                llama_pid=self.pid + 1,
                host=self.cmd[self.cmd.index("--host") + 1],
                port=port,
                model=self.cmd[self.cmd.index("--model") + 1],
            ).write(StandaloneState.path_for(self.kodo_dir, port))
        return self.exit_code


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _Network:
    """Fake ``/health`` endpoints and listening ports."""

    def __init__(self) -> None:
        self.healthy: set[str] = set()
        self.listening: set[tuple[str, int]] = set()
        self.probed: list[tuple[str, int]] = []

    def urlopen(self, url: str, timeout: float) -> _Response:
        base = url.removesuffix("/health")
        if base not in self.healthy:
            raise urllib.error.URLError("connection refused")
        return _Response(200)

    def create_connection(self, address: tuple[str, int], timeout: float) -> _Response:
        self.probed.append(address)
        if address not in self.listening:
            raise ConnectionRefusedError(address)
        return _Response(0)


class _FakeLlamaServer:
    """In-memory replacement for kodo.llms.llamacpp.LlamaServer."""

    on_start: Callable[[], None] | None = None
    start_error: Exception | None = None
    runs_for_polls: int | None = None
    stopped: list[int] = []

    def __init__(
        self,
        config: LlamaServerConfig,
        llama_args: dict[str, str] | None = None,
        profile_id: str = "",
    ) -> None:
        self.__config = config
        self.__started = False
        self.__polls = 0

    @property
    def pid(self) -> int | None:
        return 777 if self.__started else None

    @property
    def is_running(self) -> bool:
        self.__polls += 1
        limit = self.runs_for_polls
        return self.__started and (limit is None or self.__polls <= limit)

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.__started = True
        hook = _FakeLlamaServer.on_start  # read off the class: a plain function, never bound
        if hook is not None:
            hook()

    async def stop(self) -> None:
        _FakeLlamaServer.stopped.append(self.__config.port)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kodo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway ~/.kodo with llama.cpp "installed" and two custom_file models."""
    home = tmp_path / "home"
    kodo = home / ".kodo"
    (kodo / "llama.cpp").mkdir(parents=True)
    (kodo / "etc").mkdir(parents=True)
    executable = tmp_path / "llama-server"
    executable.write_text("", encoding="utf-8")
    (kodo / "llama.cpp" / "llama-meta.json").write_text(
        json.dumps({"build": 1, "executable": str(executable)}), encoding="utf-8"
    )
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    entries = [
        {"name": name, "kind": "custom_file", "path": str(gguf), "context_window": 4096}
        for name in (_MODEL, _OTHER_MODEL)
    ]
    entries += [
        {"name": "remote-model", "kind": "custom_server_url", "url": "http://localhost:1"},
        {
            "name": "missing-model",
            "kind": "custom_hf",
            "repo_id": "org/missing",
            "filename": "missing.gguf",
            "context_window": 4096,
        },
    ]
    (kodo / "etc" / "local-llm-registry.json").write_text(
        json.dumps({"entries": entries}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return kodo


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> _Processes:
    table = _Processes()
    monkeypatch.setattr("kodo.llamaserver._cli.is_pid_alive", table.is_alive)
    monkeypatch.setattr("kodo.llamaserver._cli.terminate_pid", table.terminate)
    monkeypatch.setattr("kodo.llamaserver._cli.kill_pid", table.kill)
    return table


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> _Network:
    net = _Network()
    monkeypatch.setattr(urllib.request, "urlopen", net.urlopen)
    monkeypatch.setattr(socket, "create_connection", net.create_connection)
    return net


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch, kodo_dir: Path) -> type[_FakePopen]:
    monkeypatch.setattr(_FakePopen, "instances", [])
    monkeypatch.setattr(_FakePopen, "exit_code", None)
    monkeypatch.setattr(_FakePopen, "output", b"")
    monkeypatch.setattr(_FakePopen, "delete_log", False)
    monkeypatch.setattr(_FakePopen, "ready_after_polls", None)
    monkeypatch.setattr(_FakePopen, "kodo_dir", kodo_dir)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> type[_FakeLlamaServer]:
    monkeypatch.setattr(_FakeLlamaServer, "stopped", [])
    monkeypatch.setattr(_FakeLlamaServer, "on_start", None)
    monkeypatch.setattr(_FakeLlamaServer, "start_error", None)
    monkeypatch.setattr(_FakeLlamaServer, "runs_for_polls", None)
    monkeypatch.setattr("kodo.llamaserver._supervisor.LlamaServer", _FakeLlamaServer)
    return _FakeLlamaServer


def _write_state(kodo_dir: Path, port: int, *, model: str, supervisor: int, llama: int) -> Path:
    path = StandaloneState.path_for(kodo_dir, port)
    StandaloneState(
        supervisor_pid=supervisor, llama_pid=llama, host="127.0.0.1", port=port, model=model
    ).write(path)
    return path


# ---------------------------------------------------------------------------
# start — validation and an already-occupied port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "message"),
    [
        ("no-such-model", "Unknown local model"),
        ("remote-model", "link to an external server"),
        ("missing-model", "is not installed"),
    ],
)
def test_start_rejects_a_model_it_cannot_launch(
    kodo_dir: Path,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
    model: str,
    message: str,
) -> None:
    assert main(["start", "--model", model, "--port", "9100"]) == 1
    assert message in capsys.readouterr().err
    assert popen.instances == []


def test_start_is_a_no_op_when_the_same_model_is_already_healthy(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_state(kodo_dir, 9101, model=_MODEL, supervisor=10, llama=11)
    processes.alive |= {10, 11}
    network.healthy.add("http://127.0.0.1:9101")

    assert main(["start", "--model", _MODEL, "--port", "9101"]) == 0
    assert "already served at http://127.0.0.1:9101" in capsys.readouterr().out
    assert popen.instances == []
    assert processes.terminated == []


def test_start_refuses_a_different_model_without_replace(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_state(kodo_dir, 9102, model=_OTHER_MODEL, supervisor=20, llama=21)
    processes.alive |= {20, 21}

    assert main(["start", "--model", _MODEL, "--port", "9102"]) == 1
    err = capsys.readouterr().err
    assert "already serves 'other-model'" in err
    assert "--replace" in err
    assert path.exists()
    assert popen.instances == []


def test_start_with_replace_stops_the_old_model_then_launches(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    clock: _Clock,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_state(kodo_dir, 9103, model=_OTHER_MODEL, supervisor=30, llama=31)
    processes.alive |= {30, 31}
    processes.linked[31] = 30  # the supervisor exits once its llama-server is gone
    popen.ready_after_polls = 1

    assert main(["start", "--model", _MODEL, "--port", "9103", "--replace"]) == 0
    out = capsys.readouterr().out
    assert "stopped other-model on port 9103" in out
    assert f"{_MODEL} is served at http://127.0.0.1:9103 (supervisor pid 4242)" in out
    assert processes.alive == set()
    state = StandaloneState.read(StandaloneState.path_for(kodo_dir, 9103))
    assert state is not None and state.model == _MODEL


def test_start_clears_a_stale_record_and_refuses_a_port_held_by_a_stranger(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_state(kodo_dir, 9104, model=_MODEL, supervisor=40, llama=41)
    network.listening.add(("127.0.0.1", 9104))

    assert main(["start", "--model", _MODEL, "--port", "9104"]) == 1
    assert "port 9104 is already in use by another process" in capsys.readouterr().err
    assert not path.exists()
    assert popen.instances == []


# ---------------------------------------------------------------------------
# start — bind announcements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_wildcard_bind_warns_and_probes_loopback(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    popen.ready_after_polls = 1
    assert main(["start", "--model", _MODEL, "--port", "9105", "--host", host]) == 0
    assert f"warning: binding {host}:9105" in capsys.readouterr().err
    assert network.probed == [("127.0.0.1", 9105)]


def test_non_loopback_bind_announces_the_address(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen.ready_after_polls = 1
    assert main(["start", "--model", _MODEL, "--port", "9106", "--host", "172.17.0.1"]) == 0
    err = capsys.readouterr().err
    assert "info: llama-server will be reachable at 172.17.0.1:9106" in err
    assert "warning" not in err
    assert network.probed == [("172.17.0.1", 9106)]


def test_loopback_bind_announces_nothing(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen.ready_after_polls = 1
    assert main(["start", "--model", _MODEL, "--port", "9107", "--host", "localhost"]) == 0
    err = capsys.readouterr().err
    assert "warning" not in err
    assert "info:" not in err


# ---------------------------------------------------------------------------
# start — detached supervisor
# ---------------------------------------------------------------------------


def test_detached_start_launches_a_foreground_copy_of_itself_in_a_new_session(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    clock: _Clock,
    popen: type[_FakePopen],
) -> None:
    popen.ready_after_polls = 3
    assert main(["start", "--model", _MODEL, "--port", "9108"]) == 0

    (proc,) = popen.instances
    assert proc.cmd == [
        sys.executable,
        "-m",
        "kodo.llamaserver",
        "start",
        "--model",
        _MODEL,
        "--port",
        "9108",
        "--host",
        "127.0.0.1",
        "--foreground",
    ]
    assert proc.kwargs["start_new_session"] is True
    assert proc.kwargs["stdin"] == subprocess.DEVNULL
    assert (kodo_dir / "logs" / "llama-server-9108-supervisor.log").is_file()


def test_detached_start_uses_detached_process_flags_on_windows(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    popen.ready_after_polls = 1
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        code = main(["start", "--model", _MODEL, "--port", "9109"])
    assert code == 0
    (proc,) = popen.instances
    assert proc.kwargs["creationflags"] == 0x200 | 0x8
    assert "start_new_session" not in proc.kwargs


def test_detached_start_reports_an_early_exit_with_the_log_tail(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen.exit_code = 1
    popen.output = b"boom: model file is corrupt\n"
    assert main(["start", "--model", _MODEL, "--port", "9110"]) == 1
    err = capsys.readouterr().err
    assert "error: llama-server failed to start on port 9110" in err
    assert "boom: model file is corrupt" in err


@pytest.mark.parametrize("delete_log", [False, True])
def test_detached_start_early_exit_without_any_log_output(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
    delete_log: bool,
) -> None:
    popen.exit_code = 1
    popen.delete_log = delete_log
    assert main(["start", "--model", _MODEL, "--port", "9111"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "error: llama-server failed to start on port 9111"


def test_detached_start_gives_up_and_terminates_the_supervisor_after_the_timeout(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    clock: _Clock,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = clock.now
    assert main(["start", "--model", _MODEL, "--port", "9112"]) == 1
    assert "did not become ready in time" in capsys.readouterr().err
    assert processes.terminated == [4242]
    assert clock.now - started >= 300.0


def test_detached_start_ignores_a_record_stamped_by_another_supervisor(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    clock: _Clock,
    popen: type[_FakePopen],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _foreign_record() -> None:
        if not StandaloneState.path_for(kodo_dir, 9113).exists():
            _write_state(kodo_dir, 9113, model=_MODEL, supervisor=1, llama=2)

    clock.on_sleep = _foreign_record
    assert main(["start", "--model", _MODEL, "--port", "9113"]) == 1
    assert "did not become ready in time" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# start --foreground (the supervisor itself)
# ---------------------------------------------------------------------------


def test_foreground_start_returns_1_when_llama_server_exits_on_its_own(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    fake_server: type[_FakeLlamaServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, Callable[[], None]] = {}

    def _record(loop: asyncio.AbstractEventLoop, signum: int, callback: Callable[[], None]) -> None:
        handlers[signum] = callback

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", _record)
    monkeypatch.setattr(fake_server, "runs_for_polls", 0)

    assert main(["start", "--model", _MODEL, "--port", "9120", "--foreground"]) == 1
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    assert fake_server.stopped == [9120]
    assert not StandaloneState.path_for(kodo_dir, 9120).exists()


def test_foreground_start_stops_cleanly_on_a_loop_signal(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    fake_server: type[_FakeLlamaServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, Callable[[], None]] = {}

    def _record(loop: asyncio.AbstractEventLoop, signum: int, callback: Callable[[], None]) -> None:
        handlers[signum] = callback

    def _sigterm_after_start() -> None:
        handlers[signal.SIGTERM]()

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", _record)
    monkeypatch.setattr(fake_server, "on_start", _sigterm_after_start)

    assert main(["start", "--model", _MODEL, "--port", "9121", "--foreground"]) == 0
    assert not StandaloneState.path_for(kodo_dir, 9121).exists()
    assert fake_server.stopped == [9121]


def test_foreground_start_falls_back_to_plain_signal_handlers(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    fake_server: type[_FakeLlamaServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[int, Callable[[int, object], None]] = {}

    def _unsupported(loop: asyncio.AbstractEventLoop, signum: int, callback: object) -> None:
        raise NotImplementedError

    def _install(signum: int, handler: Callable[[int, object], None]) -> None:
        installed[signum] = handler

    def _ctrl_c_after_start() -> None:
        installed[signal.SIGINT](signal.SIGINT, None)

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", _unsupported)
    monkeypatch.setattr(signal, "signal", _install)
    monkeypatch.setattr(fake_server, "on_start", _ctrl_c_after_start)

    assert main(["start", "--model", _MODEL, "--port", "9122", "--foreground"]) == 0
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert fake_server.stopped == [9122]


def test_foreground_start_reports_a_llama_server_crash(
    kodo_dir: Path,
    processes: _Processes,
    network: _Network,
    fake_server: type[_FakeLlamaServer],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        asyncio.SelectorEventLoop, "add_signal_handler", lambda loop, signum, callback: None
    )
    monkeypatch.setattr(fake_server, "start_error", RuntimeError("llama-server crashed: OOM"))

    assert main(["start", "--model", _MODEL, "--port", "9123", "--foreground"]) == 1
    assert "error: llama-server crashed: OOM" in capsys.readouterr().err
    assert fake_server.stopped == [9123]


async def test_supervisor_stamps_the_full_record_while_serving(
    kodo_dir: Path, fake_server: type[_FakeLlamaServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(kodo_dir=kodo_dir, model=_MODEL, host="127.0.0.1", port=9124)
    stop = asyncio.Event()
    seen: list[StandaloneState | None] = []

    def _inspect_then_stop() -> None:
        seen.append(StandaloneState.read(supervisor.state_path))
        stop.set()

    def _after_start() -> None:
        # Runs on the next loop turn — after the supervisor stamped its record.
        asyncio.get_running_loop().call_soon(_inspect_then_stop)

    monkeypatch.setattr(fake_server, "on_start", _after_start)

    assert supervisor.state_path == StandaloneState.path_for(kodo_dir, 9124)
    assert await supervisor.run(stop) == 0
    assert seen == [
        StandaloneState(
            supervisor_pid=os.getpid(),
            llama_pid=777,
            host="127.0.0.1",
            port=9124,
            model=_MODEL,
            profile_id="",
            kodo_version=__version__,
        )
    ]
    assert not supervisor.state_path.exists()
    assert fake_server.stopped == [9124]


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_on_an_idle_port_is_a_no_op(
    kodo_dir: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop", "--port", "9130"]) == 0
    assert "nothing is running on port 9130" in capsys.readouterr().out


def test_stop_ends_llama_server_before_its_supervisor(
    kodo_dir: Path,
    processes: _Processes,
    clock: _Clock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_state(kodo_dir, 9131, model=_MODEL, supervisor=50, llama=51)
    processes.alive |= {50, 51}

    assert main(["stop", "--port", "9131"]) == 0
    assert processes.terminated == [51, 50]
    assert processes.alive == set()
    assert not path.exists()
    assert "stopped fake-model on port 9131" in capsys.readouterr().out


def test_stop_kills_a_process_that_ignores_terminate(
    kodo_dir: Path, processes: _Processes, clock: _Clock
) -> None:
    path = _write_state(kodo_dir, 9132, model=_MODEL, supervisor=60, llama=61)
    processes.alive |= {60, 61}
    processes.stubborn.add(61)
    processes.linked[61] = 60

    assert main(["stop", "--port", "9132"]) == 0
    assert processes.killed == [61]
    assert processes.alive == set()
    assert not path.exists()


def test_stop_handles_an_unstamped_runtime_record(
    kodo_dir: Path, processes: _Processes, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    path = StandaloneState.path_for(kodo_dir, 9133)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"pid": 71, "host": "127.0.0.1", "port": 9133, "model": _MODEL}),
        encoding="utf-8",
    )
    processes.alive.add(71)

    assert main(["stop", "--port", "9133"]) == 0
    assert processes.terminated == [71]
    assert not path.exists()


def test_stop_skips_processes_that_are_already_gone(
    kodo_dir: Path, processes: _Processes, clock: _Clock
) -> None:
    path = _write_state(kodo_dir, 9134, model=_MODEL, supervisor=80, llama=81)

    assert main(["stop", "--port", "9134"]) == 0
    assert processes.terminated == []
    assert not path.exists()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_with_nothing_running(
    kodo_dir: Path, processes: _Processes, network: _Network, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status"]) == 0
    assert "no standalone llama-server is running" in capsys.readouterr().out


def test_status_lists_live_servers_and_prunes_dead_records(
    kodo_dir: Path, processes: _Processes, network: _Network, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_state(kodo_dir, 9140, model=_MODEL, supervisor=90, llama=91)
    _write_state(kodo_dir, 9141, model=_OTHER_MODEL, supervisor=0, llama=92)
    dead = _write_state(kodo_dir, 9142, model=_MODEL, supervisor=93, llama=94)
    processes.alive |= {90, 92}
    network.healthy.add("http://127.0.0.1:9140")

    assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "9140\tfake-model\thttp://127.0.0.1:9140\thealthy",
        "9141\tother-model\thttp://127.0.0.1:9141\tnot responding",
    ]
    assert not dead.exists()


def test_status_json_filters_by_port(
    kodo_dir: Path, processes: _Processes, network: _Network, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_state(kodo_dir, 9150, model=_MODEL, supervisor=95, llama=96)
    _write_state(kodo_dir, 9151, model=_OTHER_MODEL, supervisor=97, llama=98)
    processes.alive |= {95, 96, 97, 98}
    network.healthy.add("http://127.0.0.1:9151")

    assert main(["status", "--port", "9151", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "model": _OTHER_MODEL,
            "host": "127.0.0.1",
            "port": 9151,
            "url": "http://127.0.0.1:9151",
            "healthy": True,
            "llama_pid": 98,
            "supervisor_pid": 97,
            "profile_id": "",
            "kodo_version": "",
        }
    ]


def test_status_treats_a_malformed_health_url_as_unhealthy(
    kodo_dir: Path,
    processes: _Processes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _bad_url(url: str, timeout: float) -> _Response:
        raise ValueError(url)

    monkeypatch.setattr(urllib.request, "urlopen", _bad_url)
    _write_state(kodo_dir, 9160, model=_MODEL, supervisor=99, llama=100)
    processes.alive.add(100)

    assert main(["status", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["healthy"] is False

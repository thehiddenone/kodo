"""``kodo-harbor`` on the host (doc/HARBOR.md): the llama-server it starts or
reuses, the Harbor/uv/Docker processes it drives, and full (non-dry) runs.

Every process is a stand-in: ``subprocess.run`` is replaced by a scripted
:class:`_Processes` that answers each command the way the real tool would, and
the llama-server's ``/props`` endpoint by a scripted ``urlopen``. Nothing here
starts Docker, Harbor, uv or a llama-server, or touches the network beyond a
loopback stand-in for the server ``--llama-url`` attaches to.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import signal
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import TracebackType

import pytest

from kodo.harbor import (
    CONTAINER_HOST,
    HARBOR_VERSION,
    HarborInvocation,
    HarborRunError,
    HostLlama,
    KodoSource,
    LlamaAccess,
    check_docker,
    find_uv,
    main,
)
from kodo.llms import get_cloud_registry, get_local_registry

# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class _Processes:
    """Answers ``subprocess.run`` by command, and records every argv it saw."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.status: object = []
        self.start_code = 0
        self.harbor: int | BaseException = 0
        self.docker_code = 0
        self.build_code = 0
        self.build_writes_wheel = True
        self.ip: str | BaseException = ""
        self.datasets: dict[str, list[str]] = {}
        self.download_dirs: list[Path] = []
        self.download_interrupt = False
        self.harbor_interrupts = 0
        self.harbor_path = ""
        self.killed = 0

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        self.calls.append(args)
        if "kodo.llamaserver" in args:
            return self.__llamaserver(args)
        if args[0] == "ip":
            if isinstance(self.ip, BaseException):
                raise self.ip
            return _done(args, 0, stdout=self.ip)
        if args[1:3] == ["info", "--format"]:
            return _done(args, self.docker_code, stderr="Cannot connect to the Docker daemon")
        if args[1:3] == ["tool", "run"] and args[-6:-4] == ["datasets", "download"]:
            return self.__download(args)
        if args[1:3] == ["tool", "run"]:
            self.harbor_path = os.environ.get("PATH", "")
            if isinstance(self.harbor, BaseException):
                raise self.harbor
            return _done(args, self.harbor)
        if args[1] == "build":
            out_dir = Path(args[args.index("--out-dir") + 1])
            if self.build_code == 0 and self.build_writes_wheel:
                (out_dir / "py_kodo-0.0.1-py3-none-any.whl").write_bytes(b"PK")
            return _done(args, self.build_code, stderr="  build backend failed  ")
        raise AssertionError(f"unexpected command: {args}")

    def commands(self, word: str) -> list[list[str]]:
        """Every recorded argv that contains *word*."""
        return [call for call in self.calls if word in call]

    def __download(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # ``harbor datasets download REF --export -o DIR`` → DIR/<dataset>/<task>/task.toml.
        reference, out_dir = args[-4], Path(args[-1])
        self.download_dirs.append(out_dir)
        if self.download_interrupt:
            out_dir.mkdir(parents=True)
            raise KeyboardInterrupt
        if reference not in self.datasets:
            return _done(args, 1, stdout=f"Error: Dataset {reference!r} not found")
        dataset_dir = out_dir / reference.partition("@")[0].split("/")[-1]
        for task in self.datasets[reference]:
            (dataset_dir / task).mkdir(parents=True)
            (dataset_dir / task / "task.toml").write_text("", encoding="utf-8")
        return _done(args, 0, stdout="Successfully downloaded")

    def __llamaserver(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        command = args[args.index("kodo.llamaserver") + 1]
        if command == "status":
            raw = self.status if isinstance(self.status, str) else json.dumps(self.status)
            return _done(args, 0, stdout=raw)
        if command == "start":
            return _done(args, self.start_code, stderr="no such gguf" if self.start_code else "")
        return _done(args, 0)


class _Popen:
    """``subprocess.Popen`` for Harbor: answered by :class:`_Processes`.

    ``processes.harbor_interrupts`` interrupts arrive while waiting, the way
    Ctrl+C reaches a waiting parent; then Harbor exits with its scripted code.
    """

    def __init__(self, processes: _Processes, argv: list[str]) -> None:
        self.__processes = processes
        self.__result = processes(argv)

    def wait(self) -> int:
        if self.__processes.harbor_interrupts > 0:
            self.__processes.harbor_interrupts -= 1
            raise KeyboardInterrupt
        return self.__result.returncode

    def kill(self) -> None:
        self.__processes.killed += 1


def _done(
    args: list[str], code: int, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=stderr)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.__body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self.__body


class _Props:
    """Answers ``urlopen`` for the llama-server's ``/props``."""

    def __init__(self) -> None:
        self.body: object = {"default_generation_settings": {"n_ctx": 4096}}
        self.error: BaseException | None = None
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float | None = None) -> _Response:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return _Response(json.dumps(self.body).encode("utf-8"))


class _Dist:
    """An ``importlib.metadata`` distribution with a chosen ``direct_url.json``."""

    def __init__(self, version: str, direct_url: str | None) -> None:
        self.version = version
        self.__direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        return self.__direct_url if filename == "direct_url.json" else None


class _RunningLlama:
    """A loopback HTTP stand-in for a running llama-server: ``/v1/models`` and ``/props``."""

    def __init__(self) -> None:
        self.models: object = {"data": []}
        self.props: object = {"default_generation_settings": {"n_ctx": 1 << 22}}
        self.paths: list[str] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — http.server's name
                stand_in.paths.append(self.path)
                body = {"/v1/models": stand_in.models, "/props": stand_in.props}.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return None

        self.__server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.__server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.__thread = Thread(target=self.__server.serve_forever, daemon=True)
        self.__thread.start()

    def serve(self, model_id: str) -> None:
        """Answer ``/v1/models`` as an unaliased llama-server serving *model_id*."""
        self.models = {"data": [{"id": model_id}]}

    def close(self) -> None:
        self.__server.shutdown()
        self.__server.server_close()
        self.__thread.join()


@pytest.fixture
def running_llama() -> Iterator[_RunningLlama]:
    stand_in = _RunningLlama()
    yield stand_in
    stand_in.close()


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> _Processes:
    fake = _Processes()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: _Popen(fake, argv))
    return fake


@pytest.fixture
def props(monkeypatch: pytest.MonkeyPatch) -> _Props:
    fake = _Props()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def _installed_as(monkeypatch: pytest.MonkeyPatch, dist: _Dist | None) -> None:
    def distribution(name: str) -> _Dist:
        if dist is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------------------------------------------------------------------------
# The host llama-server
# ---------------------------------------------------------------------------


def test_docker_desktop_reaches_the_host_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert HostLlama.default_bind() == "127.0.0.1"
    assert HostLlama.compose_overlay() is None


@pytest.mark.parametrize(
    ("ip", "bind"),
    [
        (
            "4: docker0    inet 172.18.0.1/16 brd 172.18.255.255 scope global docker0\n",
            "172.18.0.1",
        ),
        ("", "172.17.0.1"),
        (OSError("no ip"), "172.17.0.1"),
        (subprocess.TimeoutExpired(["ip"], 5), "172.17.0.1"),
    ],
)
def test_linux_binds_the_docker_bridge(
    monkeypatch: pytest.MonkeyPatch,
    processes: _Processes,
    ip: str | BaseException,
    bind: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    processes.ip = ip
    assert HostLlama.default_bind() == bind
    overlay = HostLlama.compose_overlay()
    assert overlay is not None and f'"{CONTAINER_HOST}:host-gateway"' in overlay


def test_a_server_already_serving_the_model_is_reused_and_left_running(
    processes: _Processes, props: _Props
) -> None:
    processes.status = [
        {"healthy": False, "model": "other"},
        "not a row",
        {"healthy": True, "model": "qwen"},
    ]
    llama = HostLlama("qwen", 8090, "127.0.0.1")

    access = llama.start()

    assert not llama.started
    assert (access.entry, access.context_per_slot) == ("qwen", 4096)
    assert access.host_url == "http://127.0.0.1:8090"
    assert access.container_url == f"http://{CONTAINER_HOST}:8090"
    assert props.urls == ["http://127.0.0.1:8090/props"]
    assert processes.commands("start") == []
    llama.stop()
    assert processes.commands("stop") == []


def test_a_port_serving_another_model_is_refused(processes: _Processes) -> None:
    processes.status = [{"healthy": True, "model": "other"}]
    with pytest.raises(HarborRunError, match="already serves 'other'"):
        HostLlama("qwen", 8090, "127.0.0.1").start()


@pytest.mark.parametrize("status", ["not json", {"not": "a list"}, []])
def test_a_server_this_run_starts_is_stopped_once(
    processes: _Processes, props: _Props, status: object
) -> None:
    processes.status = status
    props.body = {"n_ctx": 8192}
    llama = HostLlama("qwen", 8091, "172.17.0.1")

    access = llama.start()

    assert llama.started
    assert access.context_per_slot == 8192
    (start,) = processes.commands("start")
    assert start[start.index("--model") + 1] == "qwen"
    assert start[start.index("--host") + 1] == "172.17.0.1"
    llama.stop()
    llama.stop()
    assert not llama.started
    assert len(processes.commands("stop")) == 1


def test_a_server_that_will_not_start_is_reported(processes: _Processes) -> None:
    processes.start_code = 1
    llama = HostLlama("qwen", 8090, "127.0.0.1")
    with pytest.raises(HarborRunError, match="did not start: no such gguf"):
        llama.start()
    assert not llama.started


@pytest.mark.parametrize(
    ("body", "error", "message"),
    [
        (None, urllib.error.URLError("refused"), "Cannot read http://127.0.0.1:8090/props"),
        ({"default_generation_settings": {"n_ctx": 0}}, None, "reports no n_ctx"),
        ({"default_generation_settings": "x", "n_ctx": "big"}, None, "reports no n_ctx"),
        (["not", "props"], None, "reports no n_ctx"),
    ],
)
def test_a_server_without_a_readable_context_is_refused(
    processes: _Processes,
    props: _Props,
    body: object,
    error: BaseException | None,
    message: str,
) -> None:
    processes.status = [{"healthy": True, "model": "qwen"}]
    props.body = body
    props.error = error
    with pytest.raises(HarborRunError, match=message):
        HostLlama("qwen", 8090, "127.0.0.1").start()


# ---------------------------------------------------------------------------
# Attaching to a running llama-server (--llama-url)
# ---------------------------------------------------------------------------


def _gguf_path(entry: str, kodo_dir: Path) -> str:
    # An unaliased llama-server lists its GGUF's full path as the model id.
    return f"/models/{get_local_registry(kodo_dir)[entry].filename}"


def test_attaching_checks_the_served_gguf_and_reads_its_context(
    tmp_path: Path, running_llama: _RunningLlama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    entry = next(iter(get_local_registry(tmp_path)))
    running_llama.serve(_gguf_path(entry, tmp_path))

    access = LlamaAccess.attach(entry, f"{running_llama.url}/", tmp_path)

    assert access.entry == entry
    assert access.host_url == running_llama.url
    assert access.container_url == f"http://{CONTAINER_HOST}:{running_llama.port}"
    assert access.context_per_slot == 1 << 22
    assert {"/v1/models", "/props"} <= set(running_llama.paths)


def test_attaching_to_a_server_with_another_model_is_refused(
    tmp_path: Path, running_llama: _RunningLlama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    entry = next(iter(get_local_registry(tmp_path)))
    running_llama.serve("/models/other-model.gguf")

    with pytest.raises(HarborRunError, match=f"other-model.gguf.*not {entry!r}"):
        LlamaAccess.attach(entry, running_llama.url, tmp_path)


def test_attaching_to_a_server_with_too_small_a_context_is_refused(
    tmp_path: Path, running_llama: _RunningLlama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    entry = next(iter(get_local_registry(tmp_path)))
    running_llama.serve(_gguf_path(entry, tmp_path))
    running_llama.props = {"default_generation_settings": {"n_ctx": 512}}

    with pytest.raises(HarborRunError, match="512-token context"):
        LlamaAccess.attach(entry, running_llama.url, tmp_path)


def test_attaching_to_nothing_is_refused(
    tmp_path: Path, running_llama: _RunningLlama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    entry = next(iter(get_local_registry(tmp_path)))
    url = running_llama.url
    running_llama.close()

    with pytest.raises(HarborRunError, match="Cannot reach llama-server"):
        LlamaAccess.attach(entry, url, tmp_path)


def test_attaching_to_an_unknown_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HarborRunError, match="No local model 'nope'"):
        LlamaAccess.attach("nope", "http://192.0.2.1:8042", tmp_path)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_linux_refuses_a_loopback_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    entry = next(iter(get_local_registry(tmp_path)))
    with pytest.raises(HarborRunError, match="loopback address"):
        LlamaAccess.attach(entry, f"http://{host}:8042", tmp_path)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("127.0.0.1:8042", "is not http://HOST:PORT"),
        ("http://127.0.0.1", "is not http://HOST:PORT"),
        ("http://127.0.0.1:port", "is not http://HOST:PORT"),
        ("ftp://127.0.0.1:8042", "is not http://HOST:PORT"),
        ("http://127.0.0.1:8042/v1", "must not have a path"),
    ],
)
def test_a_malformed_url_is_refused(url: str, message: str) -> None:
    with pytest.raises(HarborRunError, match=message):
        LlamaAccess("qwen", url, 0)


# ---------------------------------------------------------------------------
# This Kodo, uv, Docker and Harbor
# ---------------------------------------------------------------------------


def test_an_uninstalled_kodo_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed_as(monkeypatch, None)
    with pytest.raises(HarborRunError, match="not installed"):
        KodoSource.detect()


@pytest.mark.parametrize(
    "direct_url",
    [
        None,
        "{not json",
        '["a", "list"]',
        json.dumps({"dir_info": {}, "url": "file:///w/kodo"}),
        json.dumps({"dir_info": {"editable": True}, "url": "https://example.invalid/kodo"}),
    ],
)
def test_a_release_install_has_no_source_root(
    monkeypatch: pytest.MonkeyPatch, direct_url: str | None
) -> None:
    _installed_as(monkeypatch, _Dist("1.2.3", direct_url))
    source = KodoSource.detect()
    assert (source.version, source.editable_root) == ("1.2.3", None)


def test_an_editable_install_points_at_its_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "my kodo"
    url = checkout.as_uri()
    _installed_as(
        monkeypatch, _Dist("0.5.30", json.dumps({"dir_info": {"editable": True}, "url": url}))
    )
    assert KodoSource.detect().editable_root == checkout


def test_only_an_editable_install_builds_a_wheel(tmp_path: Path) -> None:
    with pytest.raises(HarborRunError, match="Only an editable"):
        KodoSource("1.0").build_wheel(Path("uv"), tmp_path)


def test_building_a_wheel_returns_the_newest_one(processes: _Processes, tmp_path: Path) -> None:
    out = tmp_path / "out"
    wheel = KodoSource("0.0.1", tmp_path / "checkout").build_wheel(Path("/bin/uv"), out)
    assert wheel == out / "py_kodo-0.0.1-py3-none-any.whl"
    (build,) = processes.commands("build")
    assert build[-1] == str(tmp_path / "checkout")


def test_a_failed_build_is_reported(processes: _Processes, tmp_path: Path) -> None:
    processes.build_code = 1
    with pytest.raises(HarborRunError, match="wheel failed:\nbuild backend failed$"):
        KodoSource("0.0.1", tmp_path).build_wheel(Path("uv"), tmp_path / "out")


def test_a_build_without_a_wheel_is_reported(processes: _Processes, tmp_path: Path) -> None:
    processes.build_writes_wheel = False
    with pytest.raises(HarborRunError, match="wrote no py-kodo wheel"):
        KodoSource("0.0.1", tmp_path).build_wheel(Path("uv"), tmp_path / "out")


def test_harbor_exit_code_is_returned(processes: _Processes, tmp_path: Path) -> None:
    processes.harbor = 3
    invocation = HarborInvocation(Path("/bin/uv"), tmp_path / "job.json", "py-kodo==1", [])
    assert invocation.run() == 3
    assert processes.calls == [invocation.command()]


def test_an_interrupt_waits_for_harbor_to_wind_down(processes: _Processes, tmp_path: Path) -> None:
    processes.harbor_interrupts = 1
    invocation = HarborInvocation(Path("/bin/uv"), tmp_path / "job.json", "py-kodo==1", [])

    with pytest.raises(KeyboardInterrupt):
        invocation.run()

    assert processes.killed == 0  # Harbor finished its own Ctrl+C path


def test_a_second_interrupt_kills_harbor(processes: _Processes, tmp_path: Path) -> None:
    processes.harbor_interrupts = 2
    invocation = HarborInvocation(Path("/bin/uv"), tmp_path / "job.json", "py-kodo==1", [])

    with pytest.raises(KeyboardInterrupt):
        invocation.run()

    assert processes.killed == 1


@pytest.mark.parametrize(("platform", "windows"), [("win32", True), ("darwin", False)])
def test_windows_starts_harbor_through_the_ctrl_break_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, windows: bool
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    command = HarborInvocation(Path("uv"), tmp_path / "job.json", "py-kodo==1", []).command()

    run = command.index("run", 3)
    if windows:
        assert command[run - 3 : run - 1] == ["python", "-c"]
        assert "signal.SIGBREAK, signal.default_int_handler" in command[run - 1]
        assert "from harbor.cli.main import app" in command[run - 1]
    else:
        assert command[run - 1] == "harbor"


def test_uv_on_path_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    uv = _executable(tmp_path / "bin" / "uv")
    monkeypatch.setenv("PATH", str(uv.parent))
    assert find_uv(tmp_path / ".kodo") == uv


def test_the_bundled_uv_is_the_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    kodo_dir = tmp_path / ".kodo"
    with pytest.raises(HarborRunError, match="uv was not found"):
        find_uv(kodo_dir)
    uv = _executable(kodo_dir / "bin" / "uv" / "uv")
    manifest = {"name": "uv", "version": "0.9.0", "path": str(uv), "url": "x"}
    (kodo_dir / "bin" / "uv.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert find_uv(kodo_dir) == uv


def test_docker_must_be_installed_and_running(
    monkeypatch: pytest.MonkeyPatch, processes: _Processes, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(HarborRunError, match="not on PATH"):
        check_docker([])
    docker = _executable(tmp_path / "bin" / "docker")
    processes.docker_code = 1
    with pytest.raises(HarborRunError, match="not reachable: Cannot connect"):
        check_docker([])
    processes.docker_code = 0
    assert check_docker([]) == docker


def test_docker_off_path_is_found_where_docker_installs_it(
    monkeypatch: pytest.MonkeyPatch, processes: _Processes, tmp_path: Path
) -> None:
    # A process started before Docker was installed has a PATH without it.
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    install_dir = tmp_path / "docker-desktop" / "bin"
    with pytest.raises(HarborRunError, match="restart"):
        check_docker([install_dir])
    docker = _executable(install_dir / "docker")
    assert check_docker([tmp_path / "elsewhere", install_dir]) == docker
    assert processes.commands("info") == [[str(docker), "info", "--format", "{{.ServerVersion}}"]]


# ---------------------------------------------------------------------------
# The CLI, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    _executable(tmp_path / "bin" / "uv")
    _executable(tmp_path / "bin" / "docker")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.chdir(tmp_path)
    _installed_as(monkeypatch, _Dist("0.5.30", None))
    return home


def _local_model(home: Path) -> str:
    return next(iter(get_local_registry(home / ".kodo")))


def _run_args(home: Path, *extra: str) -> list[str]:
    return ["run", "--model", _local_model(home), "--dataset", "hello-world@1.0", *extra]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_a_local_run_serves_the_model_runs_harbor_and_reports(
    home: Path,
    processes: _Processes,
    props: _Props,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    task_dir = home.parent / "my-task"
    task_dir.mkdir()
    processes.harbor = 1
    argv = _run_args(home, "--task", "org/name@v1", "--path", str(task_dir))
    code = main([*argv, "--llama-bind", "127.0.0.1", "--job-name", "j", "--", "-y"])

    assert code == 1
    job = home.parent / "jobs" / "j"
    assert (job / "kodo-compose.yaml").is_file() is (platform == "linux")
    assert (job / "kodo-summary.md").is_file()
    assert json.loads((job / "kodo-summary.json").read_text(encoding="utf-8"))["arms"] == {}
    config = json.loads((job / "kodo-job.json").read_text(encoding="utf-8"))
    assert {"name": "org/name", "ref": "v1"} in config["tasks"]
    (harbor,) = processes.commands("harbor")
    assert harbor[-1] == "-y"
    assert "py-kodo==0.5.30" in harbor
    assert len(processes.commands("stop")) == 1
    assert f"summary {job / 'kodo-summary.md'}" in capsys.readouterr().out


def test_an_interrupted_run_still_reports(home: Path, processes: _Processes, props: _Props) -> None:
    processes.harbor_interrupts = 1
    code = main(_run_args(home, "--llama-bind", "127.0.0.1", "--job-name", "j", "--keep-llama"))

    assert code == 130
    assert (home.parent / "jobs" / "j" / "kodo-summary.md").is_file()
    assert processes.commands("stop") == []


def test_an_attached_run_neither_starts_nor_stops_the_server(
    home: Path,
    processes: _Processes,
    running_llama: _RunningLlama,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    running_llama.serve(_gguf_path(_local_model(home), home / ".kodo"))

    code = main(_run_args(home, "--llama-url", running_llama.url, "--job-name", "j"))

    assert code == 0
    assert processes.commands("kodo.llamaserver") == []
    assert len(processes.commands("harbor")) == 1
    config = (home.parent / "jobs" / "j" / "kodo-job.json").read_text(encoding="utf-8")
    assert f"http://{CONTAINER_HOST}:{running_llama.port}" in config


def test_a_failed_attach_creates_no_job(
    home: Path,
    processes: _Processes,
    running_llama: _RunningLlama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    running_llama.serve("/models/other-model.gguf")

    code = main(_run_args(home, "--llama-url", running_llama.url, "--job-name", "j"))

    assert code == 2
    assert "other-model.gguf" in capsys.readouterr().err
    assert not (home.parent / "jobs" / "j").exists()
    assert processes.commands("harbor") == []


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        (("--llama-port", "8090"), "--llama-port"),
        (("--llama-bind", "127.0.0.1"), "--llama-bind"),
        (("--keep-llama",), "--keep-llama"),
    ],
)
def test_llama_url_excludes_the_standalone_server_flags(
    home: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...], named: str
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(_run_args(home, "--llama-url", "http://127.0.0.1:8042", *extra))
    assert exited.value.code == 2
    assert f"--llama-url cannot be combined with {named}" in capsys.readouterr().err


def test_llama_url_needs_a_local_model(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = get_cloud_registry()["anthropic"][0].model_id
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    argv = ["run", "--model", f"anthropic/{model}", "--dataset", "hello-world@1.0"]

    assert main([*argv, "--llama-url", "http://127.0.0.1:8042", "--dry-run"]) == 2
    assert "--llama-url is for a local model" in capsys.readouterr().err


def test_ctrl_break_interrupts_a_run_on_windows(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: dict[int, object] = {}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(signal, "signal", lambda signum, h: installed.update({signum: h}))

    # The handler is in place before the run starts; this run stops at --model.
    assert main(["run", "--model", "no-such-model", "--dataset", "hello-world@1.0"]) == 2

    handler = installed.get(21)
    assert callable(handler)
    with pytest.raises(KeyboardInterrupt):
        handler(21, None)


def test_a_run_hands_harbor_a_docker_found_off_path(
    home: Path, processes: _Processes, props: _Props, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Docker Desktop's per-user macOS install, in a process started before it.
    monkeypatch.setattr(sys, "platform", "darwin")
    (home.parent / "bin" / "docker").unlink()
    docker = _executable(home / ".docker" / "bin" / "docker")

    code = main(_run_args(home, "--llama-bind", "127.0.0.1", "--job-name", "j"))

    assert code == 0
    assert processes.commands("info")[0][0] == str(docker)
    assert processes.harbor_path.split(os.pathsep)[0] == str(docker.parent)


def test_a_run_without_docker_fails_before_anything_starts(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    processes.docker_code = 1
    code = main(_run_args(home, "--llama-bind", "127.0.0.1", "--job-name", "j"))

    assert code == 2
    assert "Docker daemon is not reachable" in capsys.readouterr().err
    assert processes.commands("kodo.llamaserver") == []


def test_an_existing_job_is_never_overwritten(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = home.parent / "jobs" / "j"
    job.mkdir(parents=True)
    (job / "config.json").write_text("{}", encoding="utf-8")

    code = main(_run_args(home, "--llama-bind", "127.0.0.1", "--job-name", "j", "--dry-run"))

    assert code == 2
    assert "already exists" in capsys.readouterr().err


def test_a_user_agent_needs_the_agents_directory(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _run_args(home, "--agent", "my_agent", "--llama-bind", "127.0.0.1", "--dry-run")
    assert main(argv) == 2
    assert "'my_agent' is not built in" in capsys.readouterr().err


def test_a_given_wheel_goes_to_harbor_and_the_containers(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = home / "py_kodo-9.9.9-py3-none-any.whl"
    argv = _run_args(home, "--llama-bind", "127.0.0.1", "--job-name", "j", "--dry-run")

    assert main([*argv, "--kodo-wheel", str(wheel)]) == 2
    assert "no such file" in capsys.readouterr().err

    wheel.write_bytes(b"PK")
    assert main([*argv, "--job-name", "k", "--kodo-wheel", str(wheel)]) == 0
    assert f"--with {wheel}" in capsys.readouterr().out


def test_an_editable_checkout_is_built_into_the_measured_wheel(
    home: Path,
    processes: _Processes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = home.parent / "checkout"
    direct_url = json.dumps({"dir_info": {"editable": True}, "url": checkout.as_uri()})
    _installed_as(monkeypatch, _Dist("0.5.30", direct_url))

    assert main(_run_args(home, "--llama-bind", "127.0.0.1", "--dry-run")) == 0

    (job,) = (home.parent / "jobs").iterdir()
    assert job.name.startswith("kodo_problem_solver__")
    wheel = job / "py_kodo-0.0.1-py3-none-any.whl"
    assert wheel.is_file()
    out = capsys.readouterr().out
    assert f"building a py-kodo wheel from {checkout}" in out
    assert f"--with {wheel}" in out


def test_suites_are_listed_as_text(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list-suites"]) == 0
    assert any(line.startswith("smoke\t") for line in capsys.readouterr().out.splitlines())


def _suite_file(directory: Path, body: dict[str, object]) -> Path:
    path = directory / f"{body['name']}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_list_tasks_downloads_each_dataset_lists_it_and_deletes_the_download(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    processes.datasets = {
        "terminal-bench-sample@2.0": ["regex-log", "chess-best-move"],
        "swebench-verified@1.0": ["astropy__astropy-12907"],
    }

    code = main(
        ["list-tasks", "--suite", "terminal-bench-sample", "--dataset", "swebench-verified@1.0"]
    )

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "terminal-bench-sample@2.0 (2 tasks)\n  chess-best-move\n  regex-log\n"
        "\n"
        "swebench-verified@1.0 (1 task)\n  astropy__astropy-12907\n"
    )
    assert "downloading terminal-bench-sample@2.0" in captured.err
    downloads = processes.commands("download")
    assert all(f"harbor=={HARBOR_VERSION}" in call and "--export" in call for call in downloads)
    assert len(processes.download_dirs) == 2
    assert not any(directory.parent.exists() for directory in processes.download_dirs)


def test_list_tasks_ignores_a_suites_filters(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    processes.datasets = {"terminal-bench@2.0": ["fix-git", "regex-log", "sqlite-with-gcov"]}
    suite = _suite_file(
        home,
        {
            "name": "slice",
            "datasets": [
                {
                    "name": "terminal-bench",
                    "version": "2.0",
                    "task_names": ["fix-*"],
                    "exclude_task_names": ["sqlite-*"],
                    "n_tasks": 1,
                }
            ],
        },
    )

    assert main(["list-tasks", "--suite", str(suite), "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"source": "terminal-bench@2.0", "tasks": ["fix-git", "regex-log", "sqlite-with-gcov"]}
    ]


def test_list_tasks_reads_local_datasets_and_single_tasks_without_downloading(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    for task in ("alpha", "beta"):
        (home / "mine" / task).mkdir(parents=True)
        (home / "mine" / task / "task.toml").write_text("", encoding="utf-8")
    (home / "mine" / "not-a-task").mkdir()
    suite = _suite_file(
        home,
        {
            "name": "local",
            "datasets": [{"path": "mine"}],
            "tasks": [{"name": "acme/one-task", "ref": "latest"}, {"path": "mine/alpha"}],
        },
    )

    assert main(["list-tasks", "--suite", str(suite), "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"source": str((home / "mine").resolve()), "tasks": ["alpha", "beta"]},
        {"source": "tasks", "tasks": ["acme/one-task", "alpha"]},
    ]
    assert processes.commands("download") == []


def test_a_failed_download_is_reported_and_still_deleted(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    processes.datasets = {"terminal-bench-sample@2.0": ["regex-log"]}

    code = main(["list-tasks", "--dataset", "terminal-bench-sample@2.0", "--dataset", "nope@1.0"])

    assert code == 2
    captured = capsys.readouterr()
    assert "Downloading dataset nope@1.0 failed" in captured.err
    assert "'nope@1.0' not found" in captured.err
    assert captured.out == ""
    assert not any(directory.parent.exists() for directory in processes.download_dirs)


def test_ctrl_c_during_a_download_still_deletes_it(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    processes.download_interrupt = True

    assert main(["list-tasks", "--suite", "terminal-bench"]) == 130
    assert capsys.readouterr().out == ""
    (out_dir,) = processes.download_dirs
    assert not out_dir.parent.exists()


def test_a_download_with_no_task_directories_is_an_error(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    processes.datasets = {"empty@1.0": []}

    assert main(["list-tasks", "--dataset", "empty@1.0"]) == 2
    assert "downloaded no tasks for dataset empty@1.0" in capsys.readouterr().err


def test_list_tasks_needs_a_suite_or_a_dataset(
    home: Path, processes: _Processes, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list-tasks"]) == 2
    assert "at least one --suite or --dataset" in capsys.readouterr().err
    assert processes.calls == []


def test_listing_local_models_says_when_none_is_installed(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list-models", "local"]) == 0
    captured = capsys.readouterr()
    assert "no local model is installed" in captured.err
    assert captured.out == ""


def test_summarizing_a_missing_job_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["summarize", str(tmp_path / "nope")]) == 2
    assert "No job directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --stop-on-stdin-close, end to end (the Kōdo server's Stop, BENCHMARK_SESSIONS §4.4)
# ---------------------------------------------------------------------------

_INTERRUPTIBLE_UV = """#!/bin/sh
case "$*" in
  *"tool run"*)
    trap 'echo interrupted > "$KODO_TEST_MARK.int"; exit 7' INT
    echo started > "$KODO_TEST_MARK"
    while true; do sleep 0.1; done
    ;;
esac
exit 0
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_closing_stdin_interrupts_the_run_and_harbor_winds_down_first(tmp_path: Path) -> None:
    import os
    import time

    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "docker")
    uv = _executable(bin_dir / "uv")
    uv.write_text(_INTERRUPTIBLE_UV, encoding="utf-8")
    task = tmp_path / "task"
    task.mkdir()
    mark = tmp_path / "harbor-started"
    model = get_cloud_registry()["anthropic"][0].model_id
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "ANTHROPIC_API_KEY": "sk-test",
        "KODO_TEST_MARK": str(mark),
    }
    argv = [
        sys.executable, "-m", "kodo.harbor", "run",
        "--model", f"anthropic/{model}", "--path", str(task),
        "--kodo-version", "0.5.30", "--jobs-dir", str(tmp_path / "jobs"), "--job-name", "j",
        "--stop-on-stdin-close",
    ]  # fmt: skip
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not mark.exists():
            assert process.poll() is None, "kodo-harbor exited before Harbor started"
            assert time.monotonic() < deadline, "Harbor never started"
            time.sleep(0.05)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        process.wait(timeout=60)
        out = process.stdout.read().decode()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)

    assert process.returncode == 130, out
    assert (tmp_path / "harbor-started.int").read_text().strip() == "interrupted"
    assert (tmp_path / "jobs" / "j" / "kodo-summary.md").is_file()

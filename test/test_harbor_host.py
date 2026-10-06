"""``kodo-harbor`` on the host (doc/HARBOR.md): the llama-server it starts or
reuses, the Harbor/uv/Docker processes it drives, and full (non-dry) runs.

Every process is a stand-in: ``subprocess.run`` is replaced by a scripted
:class:`_Processes` that answers each command the way the real tool would, and
the llama-server's ``/props`` endpoint by a scripted ``urlopen``. Nothing here
starts Docker, Harbor, uv or a llama-server, or touches the network.
"""

from __future__ import annotations

import importlib.metadata
import json
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import TracebackType

import pytest

from kodo.harbor import (
    CONTAINER_HOST,
    HarborInvocation,
    HarborRunError,
    HostLlama,
    KodoSource,
    check_docker,
    find_uv,
    main,
)
from kodo.llms import get_local_registry

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
        if args[1:3] == ["tool", "run"]:
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

    def __llamaserver(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        command = args[args.index("kodo.llamaserver") + 1]
        if command == "status":
            raw = self.status if isinstance(self.status, str) else json.dumps(self.status)
            return _done(args, 0, stdout=raw)
        if command == "start":
            return _done(args, self.start_code, stderr="no such gguf" if self.start_code else "")
        return _done(args, 0)


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


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> _Processes:
    fake = _Processes()
    monkeypatch.setattr(subprocess, "run", fake)
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
        check_docker()
    _executable(tmp_path / "bin" / "docker")
    processes.docker_code = 1
    with pytest.raises(HarborRunError, match="not reachable: Cannot connect"):
        check_docker()
    processes.docker_code = 0
    check_docker()


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
    processes.harbor = KeyboardInterrupt()
    code = main(_run_args(home, "--llama-bind", "127.0.0.1", "--job-name", "j", "--keep-llama"))

    assert code == 130
    assert (home.parent / "jobs" / "j" / "kodo-summary.md").is_file()
    assert processes.commands("stop") == []


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
    assert main(["suites"]) == 0
    assert any(line.startswith("smoke\t") for line in capsys.readouterr().out.splitlines())


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

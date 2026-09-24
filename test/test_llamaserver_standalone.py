"""Standalone llama-server management (``kodo-llama-server``, doc/HEADLESS.md).

Exercised end to end against a small fake "llama-server" executable that
records its argv and answers ``/health``, ``/v1/models`` and ``/props`` —
the same approach as ``test_llama_server.py``: real child processes, no
private members mocked. The fake kodo home is a throwaway ``HOME`` holding a
``llama-meta.json`` (so llama.cpp counts as installed) and a ``custom_file``
registry entry (so a model counts as present without any download state).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kodo.common import Envelope
from kodo.llamaserver import StandaloneState
from kodo.llms import LocalLLMEntry, Message, TokenDelta, get_context_window, get_local_registry
from kodo.llms.llamacpp import (
    LlamaPlugin,
    LlamaServer,
    LlamaServerConfig,
    RemoteLlamaEndpoint,
    is_pid_alive,
)
from kodo.llms.local import FileRole, FileStatus, LocalModelManager

_FAKE_SERVER = r"""
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

argv = sys.argv[1:]
def arg(name, default=""):
    return argv[argv.index(name) + 1] if name in argv else default

record = os.environ.get("FAKE_LLAMA_ARGV_FILE")
if record:
    with open(record, "w", encoding="utf-8") as fh:
        json.dump(argv, fh)

# Like the real llama-server: an unaliased model is listed under its GGUF path.
alias = arg("--alias", arg("--model"))
n_ctx = int(os.environ.get("FAKE_LLAMA_N_CTX", "4096"))
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
    def do_GET(self):
        if self.path == "/health":
            body = {"status": "ok"}
        elif self.path == "/v1/models":
            body = {"data": [{"id": alias}]}
        elif self.path == "/props":
            body = {"default_generation_settings": {"n_ctx": n_ctx}, "model_path": arg("--model")}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass

server = HTTPServer((arg("--host", "127.0.0.1"), int(arg("--port"))), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
server.serve_forever()
"""


def _make_fake_executable(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake-llama-server.py"
    script_path.write_text(_FAKE_SERVER, encoding="utf-8")
    if sys.platform == "win32":
        path = tmp_path / "fake-llama-server.bat"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8")
        return path
    path = tmp_path / "fake-llama-server"
    path.write_text(f"#!/usr/bin/env python3\n{_FAKE_SERVER}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _fake_home(tmp_path: Path, *, model: str = "fake-model") -> Path:
    """A throwaway HOME whose ~/.kodo has llama.cpp "installed" and one model."""
    home = tmp_path / "home"
    kodo_dir = home / ".kodo"
    (kodo_dir / "llama.cpp").mkdir(parents=True)
    (kodo_dir / "etc").mkdir(parents=True)
    executable = _make_fake_executable(tmp_path)
    (kodo_dir / "llama.cpp" / "llama-meta.json").write_text(
        json.dumps({"build": 1, "executable": str(executable)}), encoding="utf-8"
    )
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    (kodo_dir / "etc" / "local-llm-registry.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "name": model,
                        "kind": "custom_file",
                        "path": str(gguf),
                        "context_window": 4096,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return home


def _cli(home: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["FAKE_LLAMA_ARGV_FILE"] = str(home / "argv.json")
    return subprocess.run(
        [sys.executable, "-m", "kodo.llamaserver", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# LlamaServerConfig overrides
# ---------------------------------------------------------------------------


async def test_config_overrides_redirect_runtime_file_logs_and_set_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_LLAMA_ARGV_FILE", str(argv_file))
    kodo_dir = tmp_path / ".kodo"
    runtime_file = tmp_path / "standalone" / "state.json"
    log_file = tmp_path / "logs" / "custom.log"
    config = LlamaServerConfig(
        executable=_make_fake_executable(tmp_path),
        model_path=tmp_path / "model.gguf",
        kodo_dir=kodo_dir,
        model_name="fake-model",
        port=_free_port(),
        runtime_file=runtime_file,
        log_file=log_file,
        alias="fake-model",
    )
    server = LlamaServer(config)
    await server.start()
    try:
        recorded = json.loads(runtime_file.read_text(encoding="utf-8"))
        assert recorded["pid"] == server.pid
        assert not (kodo_dir / "llama.cpp" / "llama-server.json").exists()
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        assert argv[argv.index("--alias") + 1] == "fake-model"
        assert argv[argv.index("--log-file") + 1] == str(log_file)
    finally:
        await server.stop()
    assert not runtime_file.exists()


async def test_default_config_passes_no_alias_and_uses_shared_runtime_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_LLAMA_ARGV_FILE", str(argv_file))
    kodo_dir = tmp_path / ".kodo"
    config = LlamaServerConfig(
        executable=_make_fake_executable(tmp_path),
        model_path=tmp_path / "model.gguf",
        kodo_dir=kodo_dir,
        model_name="fake-model",
        port=_free_port(),
    )
    server = LlamaServer(config)
    await server.start()
    try:
        assert (kodo_dir / "llama.cpp" / "llama-server.json").is_file()
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        assert "--alias" not in argv
        assert argv[argv.index("--log-file") + 1] == str(kodo_dir / "logs" / "llama-server.log")
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Read-only model lookup
# ---------------------------------------------------------------------------


def test_peek_model_path_never_rewrites_an_in_flight_download(tmp_path: Path) -> None:
    models = tmp_path / "models"
    (models / "done").mkdir(parents=True)
    (models / "done" / "done.gguf").write_bytes(b"GGUF")
    state = {
        "done": {
            "repo_id": "org/done",
            "files": [
                {
                    "filename": "done.gguf",
                    "role": FileRole.MAIN.value,
                    "repo_id": "org/done",
                    "size": 4,
                    "downloaded_bytes": 4,
                    "status": FileStatus.COMPLETED.value,
                }
            ],
        },
        "busy": {
            "repo_id": "org/busy",
            "files": [
                {
                    "filename": "busy.gguf",
                    "role": FileRole.MAIN.value,
                    "repo_id": "org/busy",
                    "size": 100,
                    "downloaded_bytes": 10,
                    "status": FileStatus.DOWNLOADING.value,
                }
            ],
        },
    }
    state_path = models / "manager-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()

    assert LocalModelManager.peek_model_path(models, "done") == models / "done" / "done.gguf"
    assert LocalModelManager.peek_model_path(models, "busy") is None
    assert LocalModelManager.peek_model_path(models, "unknown") is None
    assert state_path.read_bytes() == before


# ---------------------------------------------------------------------------
# kodo-llama-server CLI
# ---------------------------------------------------------------------------


def test_start_status_restart_stop_lifecycle(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    port = _free_port()

    started = _cli(home, "start", "--model", "fake-model", "--port", str(port))
    assert started.returncode == 0, started.stderr
    state = StandaloneState.read(StandaloneState.path_for(home / ".kodo", port))
    assert state is not None and state.is_stamped
    assert state.model == "fake-model"
    try:
        # Never the kodo server's own runtime file: nothing may adopt this server.
        assert not (home / ".kodo" / "llama.cpp" / "llama-server.json").exists()
        argv = json.loads((home / "argv.json").read_text(encoding="utf-8"))
        assert argv[argv.index("--alias") + 1] == "fake-model"

        status = _cli(home, "status", "--json")
        rows = json.loads(status.stdout)
        assert [(r["port"], r["model"], r["healthy"]) for r in rows] == [(port, "fake-model", True)]

        again = _cli(home, "start", "--model", "fake-model", "--port", str(port))
        assert again.returncode == 0
        assert "already served" in again.stdout
    finally:
        stopped = _cli(home, "stop", "--port", str(port))

    assert stopped.returncode == 0, stopped.stderr
    assert _wait_gone(state.llama_pid)
    assert _wait_gone(state.supervisor_pid)
    assert not StandaloneState.path_for(home / ".kodo", port).exists()
    assert json.loads(_cli(home, "status", "--json").stdout) == []


def test_start_refuses_a_different_model_on_a_busy_port_without_replace(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    registry = home / ".kodo" / "etc" / "local-llm-registry.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["entries"].append({**data["entries"][0], "name": "other-model"})
    registry.write_text(json.dumps(data), encoding="utf-8")
    port = _free_port()

    assert _cli(home, "start", "--model", "fake-model", "--port", str(port)).returncode == 0
    try:
        refused = _cli(home, "start", "--model", "other-model", "--port", str(port))
        assert refused.returncode == 1
        assert "--replace" in refused.stderr
    finally:
        _cli(home, "stop", "--port", str(port))


def test_start_rejects_unknown_model_without_spawning(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    port = _free_port()
    result = _cli(home, "start", "--model", "no-such-model", "--port", str(port))
    assert result.returncode == 1
    assert "Unknown local model" in result.stderr
    assert not StandaloneState.path_for(home / ".kodo", port).exists()


def test_wildcard_bind_warns_but_loopback_does_not(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    port = _free_port()
    loopback = _cli(home, "start", "--model", "fake-model", "--port", str(port))
    try:
        assert "warning" not in loopback.stderr
    finally:
        _cli(home, "stop", "--port", str(port))

    port = _free_port()
    wildcard = _cli(
        home, "start", "--model", "fake-model", "--port", str(port), "--host", "0.0.0.0"
    )
    try:
        assert wildcard.returncode == 0, wildcard.stderr
        assert "warning: binding 0.0.0.0" in wildcard.stderr
    finally:
        _cli(home, "stop", "--port", str(port))


def test_stop_on_idle_port_is_a_no_op(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    result = _cli(home, "stop", "--port", str(_free_port()))
    assert result.returncode == 0
    assert "nothing is running" in result.stdout


# ---------------------------------------------------------------------------
# Attaching to a running llama-server by URL (RemoteLlamaEndpoint)
# ---------------------------------------------------------------------------


class _NullSink:
    async def send(self, env: Envelope) -> None:
        return None


@pytest.fixture
def remote_kodo_dir(tmp_path: Path) -> Path:
    """A kodo home with one registry entry and NO llama.cpp install at all."""
    kodo_dir = _fake_home(tmp_path) / ".kodo"
    (kodo_dir / "llama.cpp" / "llama-meta.json").unlink()
    return kodo_dir


@pytest.fixture(autouse=True)
def _reset_remote_endpoint() -> Iterator[None]:
    yield
    RemoteLlamaEndpoint.configure(None)


def _serve(
    tmp_path: Path, *, alias: str | None, model_file: str, n_ctx: int
) -> tuple[subprocess.Popen[bytes], str]:
    port = _free_port()
    args = [str(_make_fake_executable(tmp_path)), "--port", str(port), "--model", model_file]
    if alias is not None:
        args += ["--alias", alias]
    env = {**os.environ, "FAKE_LLAMA_N_CTX": str(n_ctx)}
    proc = subprocess.Popen(args, env=env)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
            return proc, url
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("fake llama-server never listened")


@pytest.fixture
def serve(tmp_path: Path) -> Iterator[Callable[..., str]]:
    procs: list[subprocess.Popen[bytes]] = []

    def _start(*, alias: str | None, model_file: str, n_ctx: int) -> str:
        proc, url = _serve(tmp_path, alias=alias, model_file=model_file, n_ctx=n_ctx)
        procs.append(proc)
        return url

    yield _start
    for proc in procs:
        proc.kill()
        proc.wait()


def _entry(kodo_dir: Path) -> LocalLLMEntry:
    entry = get_local_registry(kodo_dir)["fake-model"]
    assert entry.kind == "custom_file", "fixture assumes a custom_file entry"
    return entry


async def test_verify_accepts_the_aliased_model(
    remote_kodo_dir: Path, serve: Callable[..., str]
) -> None:
    window = get_context_window("fake-model", remote_kodo_dir)
    url = serve(alias="fake-model", model_file="/elsewhere/other.gguf", n_ctx=window)
    RemoteLlamaEndpoint.configure(url)
    await RemoteLlamaEndpoint.verify(_entry(remote_kodo_dir), remote_kodo_dir)


async def test_verify_accepts_an_unaliased_server_by_gguf_file_name(
    remote_kodo_dir: Path, serve: Callable[..., str]
) -> None:
    window = get_context_window("fake-model", remote_kodo_dir)
    url = serve(alias=None, model_file="/host/models/model.gguf", n_ctx=window)
    RemoteLlamaEndpoint.configure(url)
    await RemoteLlamaEndpoint.verify(_entry(remote_kodo_dir), remote_kodo_dir)


async def test_verify_rejects_a_different_model(
    remote_kodo_dir: Path, serve: Callable[..., str]
) -> None:
    window = get_context_window("fake-model", remote_kodo_dir)
    url = serve(alias="some-other-model", model_file="/x/other.gguf", n_ctx=window)
    RemoteLlamaEndpoint.configure(url)
    with pytest.raises(RuntimeError, match="not 'fake-model'"):
        await RemoteLlamaEndpoint.verify(_entry(remote_kodo_dir), remote_kodo_dir)


async def test_verify_rejects_a_smaller_context_than_the_registry_budgets_for(
    remote_kodo_dir: Path, serve: Callable[..., str]
) -> None:
    window = get_context_window("fake-model", remote_kodo_dir)
    url = serve(alias="fake-model", model_file="/x/model.gguf", n_ctx=window // 2)
    RemoteLlamaEndpoint.configure(url)
    with pytest.raises(RuntimeError, match="context"):
        await RemoteLlamaEndpoint.verify(_entry(remote_kodo_dir), remote_kodo_dir)


async def test_verify_reports_an_unreachable_endpoint(remote_kodo_dir: Path) -> None:
    RemoteLlamaEndpoint.configure(f"http://127.0.0.1:{_free_port()}")
    with pytest.raises(RuntimeError, match="Cannot reach"):
        await RemoteLlamaEndpoint.verify(_entry(remote_kodo_dir), remote_kodo_dir)


async def test_plugin_streams_from_the_endpoint_without_any_local_llama_install(
    remote_kodo_dir: Path, serve: Callable[..., str]
) -> None:
    window = get_context_window("fake-model", remote_kodo_dir)
    RemoteLlamaEndpoint.configure(serve(alias="fake-model", model_file="/x/m.gguf", n_ctx=window))
    plugin = LlamaPlugin(sink=_NullSink(), kodo_dir=remote_kodo_dir)
    events = [
        event
        async for event in plugin.stream_query(
            stream_id="s1",
            model="fake-model",
            system="",
            messages=[Message(role="user", content="hello")],
            tools=[],
            cache_breakpoints=[],
        )
    ]
    assert "hi" in "".join(e.text for e in events if isinstance(e, TokenDelta))
    assert LlamaServer.get_active_llama_server() is None or not (
        LlamaServer.get_active_llama_server().is_running  # type: ignore[union-attr]
    )


async def test_verify_accepts_an_unaliased_split_gguf_whose_registry_filename_has_a_subdir(
    remote_kodo_dir: Path, serve: Callable[..., str]
) -> None:
    registry = remote_kodo_dir / "etc" / "local-llm-registry.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["entries"].append(
        {
            "name": "split-model",
            "kind": "custom_hf",
            "repo_id": "org/split",
            "filename": "Split-bf16/Split-bf16-00001-of-00002.gguf",
            "context_window": 4096,
        }
    )
    registry.write_text(json.dumps(data), encoding="utf-8")
    entry = get_local_registry(remote_kodo_dir)["split-model"]
    window = get_context_window("split-model", remote_kodo_dir)
    url = serve(
        alias=None,
        model_file="/host/models/split-model/Split-bf16/Split-bf16-00001-of-00002.gguf",
        n_ctx=window,
    )
    RemoteLlamaEndpoint.configure(url)
    await RemoteLlamaEndpoint.verify(entry, remote_kodo_dir)

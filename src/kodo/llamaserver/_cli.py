"""``kodo-llama-server`` — start, stop and inspect a standalone llama-server.

::

    kodo-llama-server start  --model ENTRY --port N [--host 127.0.0.1] [--foreground] [--replace]
    kodo-llama-server stop   --port N
    kodo-llama-server status [--port N] [--json]

``ENTRY`` is a local-registry entry name, which already names LLM + quant.
The launch flags come from that entry's *active profile* in
``~/.kodo/etc/local-llm-registry.json`` — the same file a containerized
``kodo-headless`` reads — so host and container always agree on the model's
context window and thinking family (doc/HEADLESS.md).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from kodo.llms.llamacpp import is_pid_alive, kill_pid, terminate_pid
from kodo.project import kodo_user_dir

from ._state import StandaloneState
from ._supervisor import StandaloneError, Supervisor, resolve_standalone_entry

__all__ = ["main"]

_log = logging.getLogger(__name__)

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_READY_TIMEOUT_SECONDS = 300.0
_READY_POLL_SECONDS = 0.25
_STOP_GRACE_SECONDS = 10.0
_LOG_TAIL_CHARS = 4000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kodo-llama-server",
        description="Start, stop and inspect a standalone llama-server for a local-registry model.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Launch a model (detached unless --foreground).")
    start.add_argument("--model", required=True, help="Local-registry entry name (LLM + quant).")
    start.add_argument("--port", type=int, required=True, help="TCP port to serve on.")
    start.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    start.add_argument(
        "--foreground",
        action="store_true",
        help="Stay attached; stop the server on Ctrl+C / SIGTERM.",
    )
    start.add_argument(
        "--replace",
        action="store_true",
        help="Stop a different model already served on --port first.",
    )

    stop = commands.add_parser("stop", help="Stop the server on a port.")
    stop.add_argument("--port", type=int, required=True)

    status = commands.add_parser("status", help="Show standalone servers.")
    status.add_argument("--port", type=int, help="Only this port.")
    status.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the ``kodo-llama-server`` CLI.

    Args:
        argv (list[str] | None): Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code — ``0`` success, ``1`` failure.
    """
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    kodo_dir = kodo_user_dir()
    if args.command == "start":
        return _start(kodo_dir, args.model, args.host, args.port, args.foreground, args.replace)
    if args.command == "stop":
        return _stop(kodo_dir, args.port)
    return _status(kodo_dir, args.port, args.json)


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _start(
    kodo_dir: Path, model: str, host: str, port: int, foreground: bool, replace: bool
) -> int:
    try:
        resolve_standalone_entry(kodo_dir, model)
    except StandaloneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    path = StandaloneState.path_for(kodo_dir, port)
    existing = StandaloneState.read(path)
    if existing is not None and _is_alive(existing):
        if existing.model == model and _healthy(existing):
            print(f"{model} is already served at {existing.base_url}")
            return 0
        if not replace:
            print(
                f"error: port {port} already serves {existing.model!r}; "
                "stop it first or pass --replace",
                file=sys.stderr,
            )
            return 1
        _stop(kodo_dir, port)
    elif existing is not None:
        path.unlink(missing_ok=True)

    if _port_busy(host, port):
        print(f"error: port {port} is already in use by another process", file=sys.stderr)
        return 1

    _announce_bind(host, port)
    if foreground:
        return _run_foreground(kodo_dir, model, host, port)
    return _spawn_detached(kodo_dir, model, host, port)


def _announce_bind(host: str, port: int) -> None:
    if host in _WILDCARD_HOSTS:
        print(
            f"warning: binding {host}:{port} exposes an unauthenticated llama-server on "
            "every network interface. To reach Docker containers, bind the docker "
            "bridge address (e.g. 172.17.0.1) instead.",
            file=sys.stderr,
        )
    elif host not in _LOOPBACK_HOSTS:
        print(f"info: llama-server will be reachable at {host}:{port}", file=sys.stderr)


def _run_foreground(kodo_dir: Path, model: str, host: str, port: int) -> int:
    supervisor = Supervisor(kodo_dir=kodo_dir, model=model, host=host, port=port)

    async def _run() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop.set)
            except (NotImplementedError, RuntimeError):
                # Windows: no loop signal handlers. Ctrl+C still reaches us via
                # the plain handler; SIGTERM there is TerminateProcess, which is
                # why `stop` ends llama-server itself before the supervisor.
                signal.signal(signum, lambda _s, _f: loop.call_soon_threadsafe(stop.set))
        return await supervisor.run(stop)

    try:
        return asyncio.run(_run())
    except (StandaloneError, RuntimeError, TimeoutError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _spawn_detached(kodo_dir: Path, model: str, host: str, port: int) -> int:
    log_path = kodo_dir / "logs" / f"llama-server-{port}-supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "kodo.llamaserver",
        "start",
        "--model",
        model,
        "--port",
        str(port),
        "--host",
        host,
        "--foreground",
    ]
    with open(log_path, "wb") as log:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    path = StandaloneState.path_for(kodo_dir, port)
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"error: llama-server failed to start on port {port}", file=sys.stderr)
            tail = _read_tail(log_path)
            if tail:
                print(tail, file=sys.stderr)
            return 1
        state = StandaloneState.read(path)
        if state is not None and state.supervisor_pid == proc.pid:
            print(f"{model} is served at {state.base_url} (supervisor pid {proc.pid})")
            return 0
        time.sleep(_READY_POLL_SECONDS)

    print(f"error: llama-server on port {port} did not become ready in time", file=sys.stderr)
    terminate_pid(proc.pid)
    return 1


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def _stop(kodo_dir: Path, port: int) -> int:
    path = StandaloneState.path_for(kodo_dir, port)
    state = StandaloneState.read(path)
    if state is None:
        print(f"nothing is running on port {port}")
        return 0

    # llama-server first: on Windows terminating the supervisor is
    # TerminateProcess, which gives it no chance to stop its child.
    if state.llama_pid > 0:
        _end_process(state.llama_pid)
    if state.supervisor_pid > 0:
        # The supervisor notices its llama-server is gone and cleans up.
        _wait_gone(state.supervisor_pid, _STOP_GRACE_SECONDS)
        _end_process(state.supervisor_pid)
    path.unlink(missing_ok=True)
    print(f"stopped {state.model} on port {port}")
    return 0


def _end_process(pid: int) -> None:
    if not is_pid_alive(pid):
        return
    terminate_pid(pid)
    if not _wait_gone(pid, _STOP_GRACE_SECONDS):
        kill_pid(pid)
        _wait_gone(pid, _STOP_GRACE_SECONDS)


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return not is_pid_alive(pid)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _status(kodo_dir: Path, port: int | None, as_json: bool) -> int:
    rows: list[dict[str, object]] = []
    for path, state in StandaloneState.read_all(kodo_dir):
        if port is not None and state.port != port:
            continue
        if not _is_alive(state):
            path.unlink(missing_ok=True)
            continue
        rows.append(
            {
                "model": state.model,
                "host": state.host,
                "port": state.port,
                "url": state.base_url,
                "healthy": _healthy(state),
                "llama_pid": state.llama_pid,
                "supervisor_pid": state.supervisor_pid,
                "profile_id": state.profile_id,
                "kodo_version": state.kodo_version,
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no standalone llama-server is running")
    else:
        for row in rows:
            health = "healthy" if row["healthy"] else "not responding"
            print(f"{row['port']}\t{row['model']}\t{row['url']}\t{health}")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_alive(state: StandaloneState) -> bool:
    return (state.llama_pid > 0 and is_pid_alive(state.llama_pid)) or (
        state.supervisor_pid > 0 and is_pid_alive(state.supervisor_pid)
    )


def _healthy(state: StandaloneState) -> bool:
    try:
        with urllib.request.urlopen(f"{state.base_url}/health", timeout=1.0) as resp:
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_busy(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in _WILDCARD_HOSTS else host
    with contextlib.suppress(OSError), socket.create_connection((target, port), timeout=0.5):
        return True
    return False


def _read_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-_LOG_TAIL_CHARS:]

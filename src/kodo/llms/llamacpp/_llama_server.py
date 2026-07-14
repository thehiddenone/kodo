"""llama-server process manager.

Starts ``llama-server`` as a detached subprocess and manages it by PID.
No stdout/stderr is captured — llama-server logs to its own output.

On kodo restart, call :func:`find_running_server` to detect a surviving
process, then pass the result to :meth:`LlamaServer.adopt`.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import ctypes
import json
import logging
import os
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import aiohttp

__all__ = ["LlamaServer", "LlamaServerConfig", "RunningServer", "find_running_server"]

_log = logging.getLogger(__name__)

_HEALTH_POLL_INTERVAL: float = 0.5
_HEALTH_TIMEOUT: float = 120.0
_STOP_GRACE: float = 5.0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001


# ---------------------------------------------------------------------------
# Runtime state file
# ---------------------------------------------------------------------------


def _runtime_path(kodo_dir: Path) -> Path:
    return kodo_dir / "llama.cpp" / "llama-server.json"


def _write_runtime(kodo_dir: Path, pid: int, host: str, port: int, model: str) -> None:
    p = _runtime_path(kodo_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"pid": pid, "host": host, "port": port, "model": model}, indent=2),
        encoding="utf-8",
    )


def _remove_runtime(kodo_dir: Path) -> None:
    _runtime_path(kodo_dir).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PID helpers — platform-safe (see kodo/CLAUDE.md §Windows pitfalls)
# ---------------------------------------------------------------------------


def _is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> None:
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunningServer:
    """Metadata for a llama-server process detected at startup.

    Attributes:
        pid: OS process ID.
        host: Bind address the server is listening on.
        port: TCP port the server is listening on.
        model: Registry name of the model the server is running.
    """

    pid: int
    host: str
    port: int
    model: str


@dataclass(frozen=True)
class LlamaServerConfig:
    """Server-management configuration for a :class:`LlamaServer` instance.

    Deliberately holds nothing about the model's own llama.cpp launch
    behavior (context size, GPU offload, sampling/template flags, ...) — that
    is entirely the resolved flavor's job now (see :class:`LlamaFlavor` in
    :mod:`kodo.llms._local_registry`), passed to :class:`LlamaServer`
    separately as a plain ``dict[str, str]`` rather than stored on this
    dataclass, since it varies per launch while this config's fields don't.

    Attributes:
        executable: Path to the ``llama-server`` binary.
        model_path: Path to the ``.gguf`` model file.
        kodo_dir: User-level ``~/.kodo`` directory; used to write/remove the
            runtime state file that enables cross-restart detection, and to
            place the server's own log file.
        host: Bind address.  Defaults to ``'127.0.0.1'``.
        port: TCP port.  Defaults to `8042``.
    """

    executable: Path
    model_path: Path
    kodo_dir: Path
    model_name: str = ""
    host: str = "127.0.0.1"
    port: int = 8042


# ---------------------------------------------------------------------------
# Server manager
# ---------------------------------------------------------------------------


class LlamaServer:
    """Manages a ``llama-server`` process by PID.

    Lifecycle: create → :meth:`start` → use :attr:`base_url` → :meth:`stop`.
    After a kodo restart, call :meth:`adopt` with a :func:`find_running_server`
    result to take ownership of a surviving process.

    Args:
        config (LlamaServerConfig): Server-management configuration.
        llama_args (dict[str, str]): The resolved flavor's CLI flags (see
            ``kodo.llms.resolve_effective_llama_config``) — the model's own
            launch behavior, kept separate from *config* since it varies with
            the active flavor while *config* doesn't.
    """

    __active_llama_server: LlamaServer | None = None

    __config: LlamaServerConfig
    __llama_args: dict[str, str]
    __pid: int | None
    __active_host: str
    __active_port: int

    def __init__(self, config: LlamaServerConfig, llama_args: dict[str, str] | None = None) -> None:
        """Initialise without starting the subprocess.

        Args:
            config (LlamaServerConfig): Server-management configuration.
            llama_args (dict[str, str] | None): The resolved flavor's CLI
                flags, verbatim ``{flag: value}`` pairs; a bare/valueless
                flag is represented with an empty string value. ``None``
                (default) is treated as no flags at all.
        """
        self.__config = config
        self.__llama_args = dict(llama_args) if llama_args else {}
        self.__pid = None
        self.__active_host = config.host
        self.__active_port = config.port
        LlamaServer.__active_llama_server = self

    @property
    def is_running(self) -> bool:
        """``True`` if the server process is alive."""
        return self.__pid is not None and _is_pid_alive(self.__pid)

    @property
    def port(self) -> int:
        """TCP port the server is (or will be) listening on."""
        return self.__active_port

    @property
    def base_url(self) -> str:
        """Base URL of the OpenAI-compatible REST API."""
        return f"http://{self.__active_host}:{self.__active_port}"

    @property
    def model_name(self) -> str:
        """Registry name of the model the server is configured to serve."""
        return self.__config.model_name

    @classmethod
    def get_active_llama_server(cls) -> LlamaServer | None:
        return cls.__active_llama_server

    def adopt(self, running: RunningServer) -> None:
        """Take ownership of a running llama-server detected at startup.

        After adoption, :attr:`is_running` returns ``True`` and
        :meth:`stop` will gracefully terminate the process.

        Args:
            running (RunningServer): Result from :func:`find_running_server`.

        Raises:
            RuntimeError: If a process is already managed by this instance.
        """
        if self.is_running:
            raise RuntimeError("llama-server is already running")
        self.__pid = running.pid
        self.__active_host = running.host
        self.__active_port = running.port
        _log.info("Adopted llama-server pid=%d at %s", running.pid, self.base_url)

    async def start(self) -> None:
        """Launch llama-server and wait until it passes the health check.

        The process is launched with stdout/stderr discarded; use llama-server's
        own log flags if output is needed.  The PID is written to the runtime
        state file for cross-restart detection.

        Raises:
            RuntimeError: If already running or the process exits prematurely.
            TimeoutError: If the server does not become ready within
                ``_HEALTH_TIMEOUT`` seconds.
        """
        if self.is_running:
            raise RuntimeError("llama-server is already running")

        self.__active_host = self.__config.host
        self.__active_port = self.__config.port

        cmd = self.__build_command()
        _log.debug("Starting llama-server: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.__pid = proc.pid

        await self.__wait_ready()

        _write_runtime(
            self.__config.kodo_dir,
            self.__pid,
            self.__active_host,
            self.__active_port,
            self.__config.model_name,
        )
        _log.info("llama-server ready at %s (pid=%d)", self.base_url, self.__pid)

    async def stop(self) -> None:
        """Stop the managed llama-server process.

        Sends SIGTERM and polls until the process exits, then SIGKILL if it
        does not exit within ``_STOP_GRACE`` seconds.
        """
        pid = self.__pid
        if pid is None or not _is_pid_alive(pid):
            self.__pid = None
            return

        _log.debug("Stopping llama-server (pid=%d)", pid)
        _terminate_pid(pid)

        elapsed = 0.0
        while elapsed < _STOP_GRACE and _is_pid_alive(pid):
            await asyncio.sleep(0.5)
            elapsed += 0.5

        if _is_pid_alive(pid):
            _log.warning("llama-server pid=%d did not stop gracefully; killing", pid)
            _kill_pid(pid)

        self.__pid = None
        _remove_runtime(self.__config.kodo_dir)
        _log.info("llama-server stopped")

    def __build_command(self) -> list[str]:
        cfg = self.__config
        cmd: list[str] = [
            str(cfg.executable),
            "--log-timestamps",
            "--log-file",
            str(cfg.kodo_dir / "logs" / "llama-server.log"),
            "--model",
            str(cfg.model_path),
            "--host",
            cfg.host,
            "--port",
            str(cfg.port),
        ]
        # Everything model-specific (context size, GPU offload, KV cache
        # type, --jinja, ...) comes entirely from the resolved flavor — see
        # LlamaFlavor/resolve_effective_llama_config in _local_registry.py.
        # No defaults are merged in here, so there is no risk of a flavor's
        # own flag appearing twice on the command line.
        for k, v in self.__llama_args.items():
            cmd.append(k)
            if v:
                cmd.append(v)
        return cmd

    async def __wait_ready(self) -> None:
        url = f"{self.base_url}/health"
        elapsed = 0.0

        async with aiohttp.ClientSession() as session:
            while elapsed < _HEALTH_TIMEOUT:
                if not self.is_running:
                    raise RuntimeError(
                        f"llama-server (pid={self.__pid}) exited before becoming ready"
                    )
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
                        if resp.status == 200:
                            return
                except Exception:
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)
                elapsed += _HEALTH_POLL_INTERVAL

        raise TimeoutError(f"llama-server did not become ready within {_HEALTH_TIMEOUT:.0f} s")


# ---------------------------------------------------------------------------
# Startup detection
# ---------------------------------------------------------------------------


def find_running_server(kodo_dir: Path) -> RunningServer | None:
    """Detect a llama-server process left running from a previous kodo session.

    Three outcomes:

    - No runtime file → returns ``None``.
    - Runtime file present, PID alive → returns :class:`RunningServer`.
    - Runtime file present, PID stale → removes the file, returns ``None``.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        RunningServer | None: Running server metadata, or ``None``.
    """
    path = _runtime_path(kodo_dir)
    if not path.is_file():
        return None

    try:
        data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        pid = int(cast(int, data["pid"]))
        host = str(data.get("host", "127.0.0.1"))
        port = int(cast(int, data["port"]))
        model = str(data.get("model", ""))
    except Exception:
        _log.warning("Could not parse llama-server runtime file — removing")
        path.unlink(missing_ok=True)
        return None

    if _is_pid_alive(pid):
        _log.info("Detected running llama-server pid=%d at %s:%d model=%r", pid, host, port, model)
        return RunningServer(pid=pid, host=host, port=port, model=model)

    _log.info("Stale llama-server runtime file (pid=%d no longer alive) — removing", pid)
    path.unlink(missing_ok=True)
    return None

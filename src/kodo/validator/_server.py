"""Kodo server subprocess management for validation runs.

Starts the real singleton server (``python -m kodo.server``) exactly the way
the VS Code extension does — as a child process on a loopback port — but with
``HOME``/``USERPROFILE`` redirected to the run's scratch home, so the server
roots itself at the isolated ``.kodo`` prepared by :mod:`._home` and never
collides with a genuinely running singleton.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import sys
import time
from pathlib import Path

__all__ = ["ServerProcess", "ServerStartError", "build_child_env"]

_log = logging.getLogger(__name__)

_READY_POLL_SECONDS = 0.2
_TERMINATE_GRACE_SECONDS = 10.0


def build_child_env(home_dir: Path) -> dict[str, str]:
    """Build the environment for the kodo-server child rooted at *home_dir*.

    The child's ``HOME``/``USERPROFILE`` are redirected to the throwaway run
    home so the server roots itself at the isolated ``.kodo``. ``HF_HOME`` is
    pinned to the **real, global** HuggingFace cache (captured from the parent
    env before the HOME redirect), so every validation run shares one cache
    instead of re-fetching metadata under its throwaway home — this covers
    both GGUF downloads (``kodo.llms.local``, resolved through
    ``huggingface_hub``) and the session titler's own model download
    (``kodo.titling``, doc/INTERNALS.md §10c), which also resolves through
    ``huggingface_hub``. The titler additionally never re-downloads or
    re-lists its model at all once ``~/.kodo/titler`` (symlinked into every
    run, see ``_home.py``) already has it — ``start_titling`` checks its local
    manager state before doing any network call — so, unlike the old
    ``transformers``-based titler, there is no separate offline flag needed
    here to avoid repeated Hub traffic per run.

    Args:
        home_dir (Path): The prepared run home to export as ``HOME``.

    Returns:
        dict[str, str]: The child process environment.
    """
    env = dict(os.environ)
    real_home = os.environ.get("HOME") or os.path.expanduser("~")
    env["HF_HOME"] = os.environ.get("HF_HOME") or str(Path(real_home) / ".cache" / "huggingface")
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class ServerStartError(RuntimeError):
    """The kodo server subprocess failed to come up listening on its port."""


def pick_free_port() -> int:
    """Pick a currently-free loopback TCP port.

    Returns:
        int: A port that was free at probe time (usual bind race caveats).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerProcess:
    """One ``python -m kodo.server`` child process rooted at a scratch home.

    Args:
        home_dir: Directory exported as ``HOME``/``USERPROFILE`` to the child
            (must contain the prepared ``.kodo``; see :func:`clone_kodo_home`).
        port: WebSocket port; a free one is picked when omitted.
        log_level: ``--log-level`` passed to the server.
        console_log: File capturing the child's stdout+stderr; defaults to
            ``home_dir/server-console.log``.
    """

    __home_dir: Path
    __port: int
    __log_level: str
    __console_log: Path
    __process: asyncio.subprocess.Process | None

    def __init__(
        self,
        home_dir: Path,
        *,
        port: int | None = None,
        log_level: str = "INFO",
        console_log: Path | None = None,
    ) -> None:
        self.__home_dir = home_dir.resolve()
        self.__port = port if port is not None else pick_free_port()
        self.__log_level = log_level
        self.__console_log = console_log or (self.__home_dir / "server-console.log")
        self.__process = None

    @property
    def port(self) -> int:
        """The WebSocket port the server listens on."""
        return self.__port

    @property
    def ws_url(self) -> str:
        """The ``ws://`` URL of the server's WebSocket endpoint."""
        return f"ws://127.0.0.1:{self.__port}/ws"

    @property
    def running(self) -> bool:
        """True while the child process is alive."""
        return self.__process is not None and self.__process.returncode is None

    async def start(self, *, timeout: float = 30.0) -> None:
        """Spawn the server and wait until its port accepts connections.

        Args:
            timeout (float): Seconds to wait for readiness before failing.

        Raises:
            ServerStartError: If the child exits early or never starts
                listening within *timeout*.
        """
        if self.__process is not None:
            raise ServerStartError("Server already started")

        env = build_child_env(self.__home_dir)

        console = open(self.__console_log, "ab")  # noqa: SIM115 - handed to the subprocess
        try:
            self.__process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "kodo.server",
                "--port",
                str(self.__port),
                "--log-level",
                self.__log_level,
                env=env,
                stdout=console,
                stderr=console,
            )
        finally:
            console.close()
        _log.info(
            "Spawned kodo server pid=%d port=%d home=%s",
            self.__process.pid,
            self.__port,
            self.__home_dir,
        )

        deadline = time.monotonic() + timeout
        while True:
            if self.__process.returncode is not None:
                raise ServerStartError(
                    f"kodo server exited with code {self.__process.returncode} before "
                    f"listening; see {self.__console_log}"
                )
            if await self.__port_open():
                return
            if time.monotonic() >= deadline:
                await self.stop()
                raise ServerStartError(
                    f"kodo server did not listen on port {self.__port} within {timeout}s; "
                    f"see {self.__console_log}"
                )
            await asyncio.sleep(_READY_POLL_SECONDS)

    async def stop(self) -> None:
        """Terminate the child (SIGTERM, then SIGKILL after a grace period)."""
        process = self.__process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            _log.warning("kodo server pid=%d ignored SIGTERM; killing", process.pid)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def __port_open(self) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", self.__port)
        except OSError:
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

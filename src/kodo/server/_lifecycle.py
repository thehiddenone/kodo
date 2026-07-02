"""Discovery-file management and graceful shutdown for the singleton server.

The server is a singleton shared by every VS Code window.  Its presence is
advertised by the ``kodo-server`` discovery file at ``~/.kodo/kodo-server``,
which holds ``{"pid": <int>, "port": <int>}``.

Start-time contract (matches the VSIX launcher's stale-file protocol): if the
file already exists, the running server is considered **alive** iff its PID
still exists **or** its port is busy.  If either is true the new server refuses
to start (``sys.exit(1)``); otherwise the file is stale, is deleted, and a fresh
one is written for this process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable
from pathlib import Path

from kodo.project import WorkspaceLayout

_log = logging.getLogger(__name__)


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """Return ``True`` if *port* is currently accepting connections on *host*.

    Args:
        port (int): TCP port to probe.
        host (str): Loopback host to probe.

    Returns:
        bool: ``True`` if a connection succeeds (something is listening).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


class Lifecycle:
    """Manages the ``kodo-server`` discovery file and signal-driven shutdown.

    The discovery file at ``~/.kodo/kodo-server`` lets the VS Code extension
    locate the singleton server (its port) and detect a stale file (dead PID +
    free port).  Only one live server may hold it at a time.
    """

    __path: Path
    __port: int
    __shutdown_requested: bool

    def __init__(self, port: int, root: Path | None = None) -> None:
        """Initialise lifecycle management for the singleton server.

        Args:
            port (int): The TCP port this server binds to.
            root (Path | None): Home directory; defaults to ``~/.kodo``.
        """
        self.__path = WorkspaceLayout(root).server_discovery
        self.__port = port
        self.__shutdown_requested = False

    @property
    def discovery_path(self) -> Path:
        """Absolute path to the ``kodo-server`` discovery file."""
        return self.__path

    @property
    def shutdown_requested(self) -> bool:
        """``True`` after a graceful-shutdown signal has been received."""
        return self.__shutdown_requested

    def check_and_write(self) -> None:
        """Claim the discovery file, aborting if another server is live.

        Raises:
            SystemExit: If a live server already holds the discovery file
                (its PID exists or its port is busy).
        """
        self.__path.parent.mkdir(parents=True, exist_ok=True)

        existing = self.__read()
        if existing is not None:
            pid, port = existing
            if self.__is_running(pid) or port_busy(port):
                _log.error(
                    "Another kodo-server (pid=%d port=%d) is already running. "
                    "Refusing to start; stop it first or remove %s.",
                    pid,
                    port,
                    self.__path,
                )
                sys.exit(1)
            _log.warning("Removing stale discovery file (pid=%d is dead, port=%d free).", pid, port)
            self.__path.unlink(missing_ok=True)

        self.__write()
        _log.debug(
            "Discovery file written: %s (pid=%d port=%d)", self.__path, os.getpid(), self.__port
        )

    def remove(self) -> None:
        """Delete the discovery file if it still belongs to this process."""
        try:
            existing = self.__read()
            if existing is not None and existing[0] == os.getpid():
                self.__path.unlink(missing_ok=True)
                _log.debug("Discovery file removed: %s", self.__path)
        except OSError as exc:
            _log.warning("Could not remove discovery file: %s", exc)

    def install_signal_handlers(self, stop_callback: Callable[[], None]) -> None:
        """Install SIGTERM / SIGINT handlers that trigger graceful shutdown.

        Registered on the running event loop (``loop.add_signal_handler``), not
        via ``signal.signal``: a plain signal handler calling
        ``asyncio.Event.set`` appends the waiter wake-up with ``call_soon``,
        which does NOT write the loop's self-pipe — a fully idle loop (no
        connections, no timers due) stays blocked in ``select()`` and the
        server keeps running (holding the port and the discovery file) until
        unrelated I/O happens to wake it. ``add_signal_handler`` delivers the
        callback through the self-pipe, so shutdown is immediate even when
        idle. Falls back to ``signal.signal`` where the loop API is unavailable
        (Windows, or no running loop).

        Args:
            stop_callback (Callable[[], None]): Zero-argument callable invoked
                on signal. Typically ``asyncio.Event.set``.
        """

        def _trigger(name: str) -> None:
            _log.info("Received %s — initiating graceful shutdown", name)
            self.__shutdown_requested = True
            stop_callback()

        def _sync_handler(signum: int, _frame: object) -> None:
            _trigger(signal.Signals(signum).name)

        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        for sig in (signal.SIGTERM, signal.SIGINT):
            if loop is not None:
                try:
                    loop.add_signal_handler(sig, _trigger, signal.Signals(sig).name)
                    continue
                except (NotImplementedError, RuntimeError):
                    pass  # Windows / non-main thread — fall back below
            signal.signal(sig, _sync_handler)

    # ------------------------------------------------------------------
    # Discovery-file IO
    # ------------------------------------------------------------------

    def __read(self) -> tuple[int, int] | None:
        if not self.__path.exists():
            return None
        try:
            data = json.loads(self.__path.read_text(encoding="utf-8"))
            return int(data["pid"]), int(data["port"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            _log.warning("Unparseable discovery file %s — treating as stale", self.__path)
            return None

    def __write(self) -> None:
        self.__path.write_text(
            json.dumps({"pid": os.getpid(), "port": self.__port}), encoding="ascii"
        )

    @staticmethod
    def __is_running(pid: int) -> bool:
        # On Windows, os.kill(pid, 0) resolves to os.kill(pid, CTRL_C_EVENT)
        # because CTRL_C_EVENT == 0.  That calls GenerateConsoleCtrlEvent which
        # fires a real Ctrl+C into the process group and queues KeyboardInterrupt.
        # Use OpenProcess instead: any successful open means the process exists.
        if sys.platform == "win32":
            import ctypes

            _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

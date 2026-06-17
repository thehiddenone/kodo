"""PID file management and graceful shutdown for the Kōdo server."""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from kodo.project import ProjectLayout

_log = logging.getLogger(__name__)


class Lifecycle:
    """Manages the server PID file and system-signal-driven shutdown.

    The PID file at ``<project>/.kodo/server.pid`` prevents two server
    instances from binding to the same project simultaneously.
    """

    __pid_path: Path
    __shutdown_requested: bool

    def __init__(self, project_root: Path) -> None:
        """Initialise lifecycle management for a project.

        Args:
            project_root (Path): Absolute path to the Kodo project root.
        """
        self.__pid_path = ProjectLayout(project_root).server_pid
        self.__shutdown_requested = False

    @property
    def pid_path(self) -> Path:
        """Absolute path to the PID file."""
        return self.__pid_path

    @property
    def shutdown_requested(self) -> bool:
        """``True`` after a graceful-shutdown signal has been received."""
        return self.__shutdown_requested

    def check_and_write_pid(self) -> None:
        """Write the current PID to disk, aborting if another server is live.

        Creates ``.kodo/`` if absent.

        Raises:
            SystemExit: If a live server process already holds the PID file.
        """
        self.__pid_path.parent.mkdir(parents=True, exist_ok=True)

        if self.__pid_path.exists():
            existing = self.__pid_path.read_text(encoding="ascii").strip()
            if existing.isdigit():
                pid = int(existing)
                if self.__is_running(pid):
                    _log.error(
                        "Another kodo-server (PID %d) is already running for this project. "
                        "Stop it first or remove %s.",
                        pid,
                        self.__pid_path,
                    )
                    sys.exit(1)
                _log.warning("Removing stale PID file (PID %d is not running).", pid)

        self.__pid_path.write_text(str(os.getpid()), encoding="ascii")
        _log.debug("PID file written: %s", self.__pid_path)

    def remove_pid(self) -> None:
        """Delete the PID file if it still belongs to this process."""
        try:
            if self.__pid_path.exists():
                current = self.__pid_path.read_text(encoding="ascii").strip()
                if current == str(os.getpid()):
                    self.__pid_path.unlink()
                    _log.debug("PID file removed: %s", self.__pid_path)
        except OSError as exc:
            _log.warning("Could not remove PID file: %s", exc)

    def install_signal_handlers(self, stop_callback: Callable[[], None]) -> None:
        """Install SIGTERM / SIGINT handlers that trigger graceful shutdown.

        Args:
            stop_callback (Callable[[], None]): Zero-argument callable invoked
                on signal. Typically ``asyncio.Event.set``.
        """

        def _handle(signum: int, _frame: object) -> None:
            name = signal.Signals(signum).name
            _log.info("Received %s — initiating graceful shutdown", name)
            self.__shutdown_requested = True
            stop_callback()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

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

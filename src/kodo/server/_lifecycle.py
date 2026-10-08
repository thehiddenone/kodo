"""Discovery-file management and graceful shutdown for the singleton server.

The server is a singleton shared by every VS Code window.  Its presence is
advertised by the ``kodo-server`` discovery file at ``~/.kodo/kodo-server``,
which holds ``{"pid": <int>, "port": <int>, "state": <str>}``.  ``state`` walks
``"starting"`` (file claimed, port not bound yet) → ``"serving"`` (listening) →
``"stopping"`` (shutdown committed, teardown under way), and the file is
removed once teardown finishes.

Start-time contract (matches the VSIX launcher's attach policy): the file is
claimed with an exclusive create, so two servers starting at once cannot both
own it.  An existing file's server is considered **alive** iff its PID still
exists **or** its port is busy — with two refinements the ``state`` makes
possible:

* ``"stopping"`` and alive: the predecessor is on its way out.  The new server
  waits (bounded by ``handoff_timeout``) for it to exit and then takes over,
  instead of refusing to start.
* ``"serving"`` with a free port: a serving server always holds its port until
  it has advertised ``"stopping"``, so a live PID here belongs to an unrelated
  process that recycled the dead server's PID — the file is stale.

Any other live server makes the new one refuse to start (``sys.exit(1)``); a
file with no live server behind it is stale, is deleted, and is re-claimed.
A file without ``state`` (written by a server that predates the field) is
judged by PID/port alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

from kodo.project import WorkspaceLayout

_log = logging.getLogger(__name__)

# Discovery-file ``state`` values.
_STARTING = "starting"
_SERVING = "serving"
_STOPPING = "stopping"

# How often the start-up handoff re-probes a stopping predecessor.
_HANDOFF_POLL_SECONDS = 0.25
# Exclusive-create attempts before giving up on claiming the file. Each retry
# follows the deletion of a stale file, so more than a couple means another
# process keeps recreating it — something is wrong, and exiting is safer.
_CLAIM_ATTEMPTS = 5


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
    locate the singleton server (its port), tell whether it can still attach to
    it (its ``state``), and detect a stale file (dead PID + free port).  Only
    one live server may hold it at a time.
    """

    __path: Path
    __port: int
    __handoff_timeout: float
    __shutdown_requested: bool

    def __init__(self, port: int, root: Path | None = None, handoff_timeout: float = 30.0) -> None:
        """Initialise lifecycle management for the singleton server.

        Args:
            port (int): The TCP port this server binds to.
            root (Path | None): Home directory; defaults to ``~/.kodo``.
            handoff_timeout (float): How long :meth:`check_and_write` waits for
                a ``"stopping"`` predecessor to exit before giving up.
        """
        self.__path = WorkspaceLayout(root).server_discovery
        self.__port = port
        self.__handoff_timeout = handoff_timeout
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
        """Claim the discovery file (state ``"starting"``), aborting if another server is live.

        Waits for a predecessor that has advertised ``"stopping"`` to exit
        first (up to ``handoff_timeout``), so a window that relaunches the
        server while the old one is still tearing down gets a server rather
        than an exit.

        Raises:
            SystemExit: If a live server already holds the discovery file
                (its PID exists or its port is busy) and is not stopping, or a
                stopping one outlives ``handoff_timeout``.
        """
        self.__path.parent.mkdir(parents=True, exist_ok=True)

        for _ in range(_CLAIM_ATTEMPTS):
            if self.__try_claim():
                _log.debug(
                    "Discovery file written: %s (pid=%d port=%d)",
                    self.__path,
                    os.getpid(),
                    self.__port,
                )
                return
            self.__clear_or_exit()
        _log.error(
            "Could not claim %s after %d attempts. Refusing to start.", self.__path, _CLAIM_ATTEMPTS
        )
        sys.exit(1)

    def mark_serving(self) -> None:
        """Advertise that this server is listening (state ``"serving"``).

        A no-op if the file no longer belongs to this process.
        """
        self.__set_state(_SERVING)

    def mark_stopping(self) -> None:
        """Advertise that this server's shutdown is committed (state ``"stopping"``).

        Called before teardown starts, so a launcher that reads the file while
        teardown is still running waits for this process to exit instead of
        trying to attach to it. A no-op if the file no longer belongs to this
        process.
        """
        self.__set_state(_STOPPING)

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
    # Claim / liveness
    # ------------------------------------------------------------------

    def __try_claim(self) -> bool:
        """Create the file exclusively for this process; ``False`` if it already exists."""
        try:
            with self.__path.open("x", encoding="ascii") as fh:
                fh.write(self.__payload(_STARTING))
        except FileExistsError:
            return False
        return True

    def __clear_or_exit(self) -> None:
        """Deal with an existing file: wait out a stopping server, delete a stale one, or exit."""
        existing = self.__read()
        if existing is None:
            # Unparseable (or vanished between the create and the read).
            self.__path.unlink(missing_ok=True)
            return
        pid, port, state = existing
        pid_alive = self.__is_running(pid)
        busy = port_busy(port)

        if state == _STOPPING and (pid_alive or busy):
            _log.info(
                "Previous kodo-server (pid=%d port=%d) is shutting down — "
                "waiting up to %.0fs for it to exit",
                pid,
                port,
                self.__handoff_timeout,
            )
            if not self.__wait_for_exit(pid, port):
                _log.error(
                    "Previous kodo-server (pid=%d port=%d) is still shutting down after %.0fs. "
                    "Refusing to start.",
                    pid,
                    port,
                    self.__handoff_timeout,
                )
                sys.exit(1)
            _log.info("Previous kodo-server (pid=%d) exited — taking over", pid)
        elif state == _SERVING and pid_alive and not busy:
            _log.warning(
                "Discovery file says pid=%d is serving on port %d, but the port is free — "
                "the PID belongs to another process now; treating the file as stale.",
                pid,
                port,
            )
        elif pid_alive or busy:
            _log.error(
                "Another kodo-server (pid=%d port=%d) is already running. "
                "Refusing to start; stop it first or remove %s.",
                pid,
                port,
                self.__path,
            )
            sys.exit(1)
        else:
            _log.warning("Removing stale discovery file (pid=%d is dead, port=%d free).", pid, port)
        self.__remove_if_unchanged(existing)

    def __wait_for_exit(self, pid: int, port: int) -> bool:
        deadline = time.monotonic() + self.__handoff_timeout
        while self.__is_running(pid) or port_busy(port):
            if time.monotonic() >= deadline:
                return False
            time.sleep(_HANDOFF_POLL_SECONDS)
        return True

    def __remove_if_unchanged(self, judged: tuple[int, int, str | None]) -> None:
        # Only delete the file we actually judged: during a handoff wait the
        # predecessor removes its own file, and a third server may have
        # claimed a fresh one since — that one must survive.
        if self.__read() == judged:
            self.__path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Discovery-file IO
    # ------------------------------------------------------------------

    def __read(self) -> tuple[int, int, str | None] | None:
        if not self.__path.exists():
            return None
        try:
            data = json.loads(self.__path.read_text(encoding="utf-8"))
            state = data.get("state")
            return int(data["pid"]), int(data["port"]), state if isinstance(state, str) else None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError, OSError):
            _log.warning("Unparseable discovery file %s — treating as stale", self.__path)
            return None

    def __set_state(self, state: str) -> None:
        existing = self.__read()
        if existing is None or existing[0] != os.getpid():
            return
        # Written to a sibling and renamed over the file, so a reader never
        # sees it truncated. Best-effort: a failure leaves the previous state,
        # which every reader still handles (just less precisely).
        tmp = self.__path.with_name(f"{self.__path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(self.__payload(state), encoding="ascii")
            os.replace(tmp, self.__path)
        except OSError as exc:
            _log.warning("Could not mark discovery file %s as %s: %s", self.__path, state, exc)
            tmp.unlink(missing_ok=True)
            return
        _log.debug("Discovery file marked %s: %s", state, self.__path)

    def __payload(self, state: str) -> str:
        return json.dumps({"pid": os.getpid(), "port": self.__port, "state": state})

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

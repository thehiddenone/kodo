"""Behavior tests for kodo.server._lifecycle.Lifecycle.

The singleton server advertises itself via the ``kodo-server`` discovery file
(``~/.kodo/kodo-server``, JSON ``{pid, port}``).  Tests observe filesystem
side-effects only; the home dir is redirected to a temp path via the ``root``
argument so the real ``~/.kodo`` is never touched.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodo.server import Lifecycle, port_busy

_FREE_PORT = 64999  # almost certainly not listening during the test run


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture()
def lifecycle(root: Path) -> Lifecycle:
    return Lifecycle(_FREE_PORT, root=root)


def _read(path: Path) -> dict[str, int]:
    return json.loads(path.read_text(encoding="ascii"))


# ---------------------------------------------------------------------------
# discovery_path / shutdown_requested
# ---------------------------------------------------------------------------


def test_discovery_path_is_inside_kodo_dir(lifecycle: Lifecycle, root: Path) -> None:
    assert lifecycle.discovery_path == root / "kodo-server"


def test_shutdown_requested_is_false_initially(lifecycle: Lifecycle) -> None:
    assert lifecycle.shutdown_requested is False


# ---------------------------------------------------------------------------
# check_and_write
# ---------------------------------------------------------------------------


def test_check_and_write_creates_discovery_file(lifecycle: Lifecycle) -> None:
    lifecycle.check_and_write()
    assert lifecycle.discovery_path.exists()


def test_check_and_write_records_pid_and_port(lifecycle: Lifecycle) -> None:
    lifecycle.check_and_write()
    data = _read(lifecycle.discovery_path)
    assert data == {"pid": os.getpid(), "port": _FREE_PORT}


def test_check_and_write_creates_home_dir_if_absent(lifecycle: Lifecycle, root: Path) -> None:
    assert not root.exists()
    lifecycle.check_and_write()
    assert root.is_dir()


def test_check_and_write_replaces_stale_file(lifecycle: Lifecycle) -> None:
    """A dead PID + free port ⇒ the file is stale and is replaced."""
    lifecycle.discovery_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.discovery_path.write_text(
        json.dumps({"pid": 999999999, "port": _FREE_PORT}), encoding="ascii"
    )
    lifecycle.check_and_write()
    assert _read(lifecycle.discovery_path)["pid"] == os.getpid()


def test_check_and_write_exits_if_live_pid_holds_file(lifecycle: Lifecycle) -> None:
    """Our own (live) PID in the file ⇒ a server is considered running ⇒ exit 1."""
    lifecycle.discovery_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.discovery_path.write_text(
        json.dumps({"pid": os.getpid(), "port": _FREE_PORT}), encoding="ascii"
    )
    with pytest.raises(SystemExit):
        lifecycle.check_and_write()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_deletes_own_file(lifecycle: Lifecycle) -> None:
    lifecycle.check_and_write()
    assert lifecycle.discovery_path.exists()
    lifecycle.remove()
    assert not lifecycle.discovery_path.exists()


def test_remove_does_nothing_when_no_file(lifecycle: Lifecycle) -> None:
    lifecycle.remove()  # must not raise


def test_remove_keeps_file_owned_by_other_process(lifecycle: Lifecycle) -> None:
    lifecycle.discovery_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.discovery_path.write_text(
        json.dumps({"pid": 1, "port": _FREE_PORT}), encoding="ascii"
    )
    lifecycle.remove()
    assert lifecycle.discovery_path.exists()


# ---------------------------------------------------------------------------
# install_signal_handlers
# ---------------------------------------------------------------------------


def test_install_signal_handlers_sets_shutdown_requested_on_sigterm(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda signum, h: installed.update({signum: h}))

    called: list[bool] = []
    lifecycle.install_signal_handlers(lambda: called.append(True))

    handler = installed.get(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)  # type: ignore[operator]

    assert lifecycle.shutdown_requested is True
    assert called == [True]


def test_install_signal_handlers_invokes_callback_on_sigint(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda signum, h: installed.update({signum: h}))

    invocations: list[int] = []
    lifecycle.install_signal_handlers(lambda: invocations.append(1))

    handler = installed.get(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)  # type: ignore[operator]

    assert len(invocations) == 1


# ---------------------------------------------------------------------------
# port_busy
# ---------------------------------------------------------------------------


def test_port_busy_true_for_a_listening_loopback_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port_busy(port) is True


def test_port_busy_false_for_a_closed_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port_busy(port) is False


# ---------------------------------------------------------------------------
# check_and_write — more stale/live cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"pid": 1}), json.dumps({"pid": "abc", "port": 1})],
)
def test_check_and_write_replaces_an_unparseable_file(lifecycle: Lifecycle, content: str) -> None:
    lifecycle.discovery_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.discovery_path.write_text(content, encoding="ascii")

    lifecycle.check_and_write()

    assert _read(lifecycle.discovery_path) == {"pid": os.getpid(), "port": _FREE_PORT}


def test_check_and_write_exits_if_recorded_port_is_busy(root: Path) -> None:
    """A dead PID but a port something is listening on ⇒ still live ⇒ exit 1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy_port = listener.getsockname()[1]
        lifecycle = Lifecycle(_FREE_PORT, root=root)
        lifecycle.discovery_path.parent.mkdir(parents=True, exist_ok=True)
        lifecycle.discovery_path.write_text(
            json.dumps({"pid": 999999999, "port": busy_port}), encoding="ascii"
        )
        with pytest.raises(SystemExit):
            lifecycle.check_and_write()


class _FakeKernel32:
    def __init__(self, handle: int) -> None:
        self.__handle = handle
        self.closed: list[int] = []

    def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> int:  # noqa: N802
        return self.__handle

    def CloseHandle(self, handle: int) -> None:  # noqa: N802
        self.closed.append(handle)


@pytest.mark.parametrize(("handle", "live"), [(42, True), (0, False)])
def test_check_and_write_probes_liveness_via_open_process_on_windows(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, handle: int, live: bool
) -> None:
    """On Windows the liveness probe is OpenProcess, never ``os.kill(pid, 0)``."""
    kernel32 = _FakeKernel32(handle)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    def _no_kill(_pid: int, _sig: int) -> None:
        raise AssertionError("os.kill must not be used on Windows")

    monkeypatch.setattr(os, "kill", _no_kill)

    lifecycle.discovery_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.discovery_path.write_text(
        json.dumps({"pid": 123456, "port": _FREE_PORT}), encoding="ascii"
    )

    if live:
        with pytest.raises(SystemExit):
            lifecycle.check_and_write()
        assert kernel32.closed == [handle]
    else:
        lifecycle.check_and_write()
        assert _read(lifecycle.discovery_path)["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# remove — failure is logged, not raised
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
def test_remove_swallows_an_os_error(lifecycle: Lifecycle) -> None:
    lifecycle.check_and_write()
    parent = lifecycle.discovery_path.parent
    parent.chmod(0o555)  # unlink inside a read-only dir fails
    try:
        lifecycle.remove()  # must not raise
        assert lifecycle.discovery_path.exists()
    finally:
        parent.chmod(0o755)


# ---------------------------------------------------------------------------
# install_signal_handlers — inside a running event loop
# ---------------------------------------------------------------------------


async def test_install_signal_handlers_uses_the_running_loop(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    loop_handlers: dict[int, tuple[object, tuple[object, ...]]] = {}
    plain_handlers: dict[int, object] = {}

    def _add(sig: int, callback: object, *args: object) -> None:
        loop_handlers[sig] = (callback, args)

    monkeypatch.setattr(loop, "add_signal_handler", _add)
    monkeypatch.setattr(signal, "signal", lambda signum, h: plain_handlers.update({signum: h}))

    called: list[bool] = []
    lifecycle.install_signal_handlers(lambda: called.append(True))

    assert set(loop_handlers) == {signal.SIGTERM, signal.SIGINT}
    assert plain_handlers == {}
    callback, args = loop_handlers[signal.SIGTERM]
    assert callable(callback)
    callback(*args)
    assert lifecycle.shutdown_requested is True
    assert called == [True]


async def test_install_signal_handlers_falls_back_when_loop_api_unavailable(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()

    def _unsupported(_sig: int, _callback: object, *_args: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _unsupported)
    plain_handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda signum, h: plain_handlers.update({signum: h}))

    called: list[bool] = []
    lifecycle.install_signal_handlers(lambda: called.append(True))

    assert set(plain_handlers) == {signal.SIGTERM, signal.SIGINT}
    handler = plain_handlers[signal.SIGINT]
    assert callable(handler)
    handler(signal.SIGINT, None)
    assert called == [True]

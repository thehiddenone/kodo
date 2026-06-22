"""Behavior tests for kodo.server._lifecycle.Lifecycle.

The singleton server advertises itself via the ``kodo-server`` discovery file
(``~/.kodo/kodo-server``, JSON ``{pid, port}``).  Tests observe filesystem
side-effects only; the home dir is redirected to a temp path via the ``root``
argument so the real ``~/.kodo`` is never touched.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from kodo.server import Lifecycle

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

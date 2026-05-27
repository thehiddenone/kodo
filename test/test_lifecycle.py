"""Behavior tests for kodo.server._lifecycle.Lifecycle.

Tests verify PID file creation, stale-PID cleanup, removal, and signal
handler installation — all using filesystem side-effects observable without
accessing private state.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from kodo.server._lifecycle import Lifecycle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Return a fresh project root directory."""
    return tmp_path / "project"


@pytest.fixture()
def lifecycle(project: Path) -> Lifecycle:
    """Return a Lifecycle bound to the temp project."""
    return Lifecycle(project)


# ---------------------------------------------------------------------------
# pid_path property
# ---------------------------------------------------------------------------


def test_pid_path_is_inside_kodo_dir(lifecycle: Lifecycle, project: Path) -> None:
    """
    Given a Lifecycle for a project,
    when pid_path is accessed,
    then it resolves to <project>/.kodo/server.pid.
    """
    expected = project / ".kodo" / "server.pid"
    assert lifecycle.pid_path == expected


# ---------------------------------------------------------------------------
# shutdown_requested property
# ---------------------------------------------------------------------------


def test_shutdown_requested_is_false_initially(lifecycle: Lifecycle) -> None:
    """
    Given a newly created Lifecycle,
    when shutdown_requested is read,
    then it is False.
    """
    assert lifecycle.shutdown_requested is False


# ---------------------------------------------------------------------------
# check_and_write_pid
# ---------------------------------------------------------------------------


def test_check_and_write_pid_creates_pid_file(lifecycle: Lifecycle) -> None:
    """
    Given no existing PID file,
    when check_and_write_pid is called,
    then the PID file is created at pid_path.
    """
    lifecycle.check_and_write_pid()
    assert lifecycle.pid_path.exists()


def test_check_and_write_pid_writes_current_process_id(lifecycle: Lifecycle) -> None:
    """
    Given no existing PID file,
    when check_and_write_pid is called,
    then the file contains the current process PID.
    """
    lifecycle.check_and_write_pid()
    stored = lifecycle.pid_path.read_text(encoding="ascii").strip()
    assert stored == str(os.getpid())


def test_check_and_write_pid_creates_kodo_directory_if_absent(
    lifecycle: Lifecycle, project: Path
) -> None:
    """
    Given a project with no .kodo directory,
    when check_and_write_pid is called,
    then the .kodo directory is created.
    """
    assert not (project / ".kodo").exists()
    lifecycle.check_and_write_pid()
    assert (project / ".kodo").is_dir()


def test_check_and_write_pid_replaces_stale_pid_file(lifecycle: Lifecycle) -> None:
    """
    Given a PID file referencing a dead process (PID 0 is always invalid on Linux/Windows),
    when check_and_write_pid is called,
    then the file is replaced with the current PID.
    """
    lifecycle.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.pid_path.write_text("999999999", encoding="ascii")
    lifecycle.check_and_write_pid()
    stored = lifecycle.pid_path.read_text(encoding="ascii").strip()
    assert stored == str(os.getpid())


def test_check_and_write_pid_exits_if_live_process_holds_pid(
    lifecycle: Lifecycle,
) -> None:
    """
    Given a PID file containing the current process's own PID (simulating a live
    duplicate),
    when check_and_write_pid is called,
    then SystemExit is raised (only one server per project is allowed).
    """
    lifecycle.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.pid_path.write_text(str(os.getpid()), encoding="ascii")
    with pytest.raises(SystemExit):
        lifecycle.check_and_write_pid()


# ---------------------------------------------------------------------------
# remove_pid
# ---------------------------------------------------------------------------


def test_remove_pid_deletes_own_pid_file(lifecycle: Lifecycle) -> None:
    """
    Given a PID file written by check_and_write_pid,
    when remove_pid is called,
    then the PID file no longer exists.
    """
    lifecycle.check_and_write_pid()
    assert lifecycle.pid_path.exists()
    lifecycle.remove_pid()
    assert not lifecycle.pid_path.exists()


def test_remove_pid_does_nothing_when_no_pid_file(lifecycle: Lifecycle) -> None:
    """
    Given no PID file,
    when remove_pid is called,
    then no exception is raised.
    """
    lifecycle.remove_pid()  # must not raise


def test_remove_pid_does_not_delete_pid_file_owned_by_other_process(
    lifecycle: Lifecycle,
) -> None:
    """
    Given a PID file containing a different PID (not ours),
    when remove_pid is called,
    then the file is left intact.
    """
    lifecycle.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.pid_path.write_text("1", encoding="ascii")
    lifecycle.remove_pid()
    assert lifecycle.pid_path.exists()


# ---------------------------------------------------------------------------
# install_signal_handlers
# ---------------------------------------------------------------------------


def test_install_signal_handlers_sets_shutdown_requested_on_sigterm(
    lifecycle: Lifecycle,
) -> None:
    """
    Given installed signal handlers,
    when the SIGTERM handler is triggered,
    then shutdown_requested becomes True and the callback is invoked.
    """
    called: list[bool] = []

    lifecycle.install_signal_handlers(lambda: called.append(True))
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)  # type: ignore[call-arg]

    assert lifecycle.shutdown_requested is True
    assert called == [True]


def test_install_signal_handlers_invokes_callback_on_sigint(
    lifecycle: Lifecycle,
) -> None:
    """
    Given installed signal handlers,
    when the SIGINT handler is triggered,
    then the stop callback is invoked.
    """
    invocations: list[int] = []

    lifecycle.install_signal_handlers(lambda: invocations.append(1))
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)  # type: ignore[call-arg]

    assert len(invocations) == 1

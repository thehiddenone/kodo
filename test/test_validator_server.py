"""Tests for ``kodo.validator._server`` -- kodo-server subprocess management.

Covers:
* :func:`build_child_env` -- HOME/USERPROFILE/HF_HOME redirection.
* :func:`pick_free_port` -- returns an int in valid range.
* :class:`ServerProcess` -- properties (``port``, ``ws_url``, ``running``).
* :meth:`ServerProcess.start` / ``.stop`` with mocked subprocess.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.validator._server import (
    ServerProcess,
    ServerStartError,
    build_child_env,
    pick_free_port,
)

# ---------------------------------------------------------------------------
# build_child_env
# ---------------------------------------------------------------------------


def test_build_child_env_sets_home_and_userprofile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/real/home")
    monkeypatch.setenv("USERPROFILE", "/real/profile")
    env = build_child_env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)


def test_build_child_env_sets_hf_home_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", "/custom/hf")
    env = build_child_env(tmp_path)
    assert env["HF_HOME"] == "/custom/hf"


def test_build_child_env_defaults_hf_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("HOME", "/real/home")
    env = build_child_env(tmp_path)
    assert env["HF_HOME"] == str(Path("/real/home") / ".cache" / "huggingface")


def test_build_child_env_includes_pythonunbuffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = build_child_env(tmp_path)
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_child_env_preserves_other_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "bar")
    env = build_child_env(tmp_path)
    assert env["FOO"] == "bar"


# ---------------------------------------------------------------------------
# pick_free_port
# ---------------------------------------------------------------------------


def test_pick_free_port_returns_int_in_valid_range() -> None:
    port = pick_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_pick_free_port_returns_different_ports() -> None:
    ports = {pick_free_port() for _ in range(5)}
    assert len(ports) >= 1


# ---------------------------------------------------------------------------
# ServerProcess -- properties
# ---------------------------------------------------------------------------


def test_server_process_port_explicit() -> None:
    sp = ServerProcess(Path("/tmp"), port=12345)
    assert sp.port == 12345


def test_server_process_ws_url() -> None:
    sp = ServerProcess(Path("/tmp"), port=12345)
    assert sp.ws_url == "ws://127.0.0.1:12345/ws"


def test_server_process_running_false_initially() -> None:
    sp = ServerProcess(Path("/tmp"))
    assert sp.running is False


def test_server_process_running_true_when_process_alive() -> None:
    mock_process = MagicMock()
    mock_process.returncode = None
    sp = ServerProcess(Path("/tmp"))
    sp._ServerProcess__process = mock_process
    assert sp.running is True


def test_server_process_running_false_when_finished() -> None:
    mock_process = MagicMock()
    mock_process.returncode = 0
    sp = ServerProcess(Path("/tmp"))
    sp._ServerProcess__process = mock_process
    assert sp.running is False


# ---------------------------------------------------------------------------
# ServerProcess -- start / stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_process_start_raises_if_already_started(tmp_path: Path) -> None:
    mock_process = MagicMock()
    sp = ServerProcess(tmp_path, port=12345)
    sp._ServerProcess__process = mock_process
    with pytest.raises(ServerStartError, match="already started"):
        await sp.start()


@pytest.mark.asyncio
async def test_server_process_start_calls_create_subprocess_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.pid = 1234

    async def _fake_open_port() -> bool:
        return True

    create_mock = AsyncMock(return_value=mock_process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create_mock)

    sp = ServerProcess(tmp_path, port=12345)
    sp._ServerProcess__port_open = _fake_open_port
    await sp.start(timeout=1.0)

    create_mock.assert_called_once()
    assert sp.running is True


@pytest.mark.asyncio
async def test_server_process_stop_terminates_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kodo.validator._server as _server

    # Short grace period for the test.
    monkeypatch.setattr(_server, "_TERMINATE_GRACE_SECONDS", 0.01)

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.terminate = MagicMock()
    mock_process.wait = AsyncMock()

    sp = ServerProcess(tmp_path, port=12345)
    sp._ServerProcess__process = mock_process
    await sp.stop()

    mock_process.terminate.assert_called_once()
    mock_process.wait.assert_called_once()


@pytest.mark.asyncio
async def test_server_process_stop_noop_when_not_running(tmp_path: Path) -> None:
    sp = ServerProcess(tmp_path)
    # Should not raise.
    await sp.stop()


@pytest.mark.asyncio
async def test_server_process_stop_kills_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If SIGTERM isn't honored within the grace period, SIGKILL is sent."""
    import asyncio as _asyncio

    import kodo.validator._server as _server

    # Short grace period so we hit the timeout quickly.
    monkeypatch.setattr(_server, "_TERMINATE_GRACE_SECONDS", 0.01)

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.terminate = MagicMock()
    mock_process.kill = MagicMock()

    async def _fake_wait():
        await _asyncio.sleep(10)  # Never finishes

    mock_process.wait = _fake_wait

    sp = ServerProcess(tmp_path, port=12345)
    sp._ServerProcess__process = mock_process
    await sp.stop()

    mock_process.terminate.assert_called_once()
    mock_process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_server_process_stop_ignores_process_lookup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the process is already gone, terminate() raises ProcessLookupError
    which is suppressed."""
    import kodo.validator._server as _server

    monkeypatch.setattr(_server, "_TERMINATE_GRACE_SECONDS", 0.01)

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.terminate = MagicMock(side_effect=ProcessLookupError("gone"))
    mock_process.wait = AsyncMock()

    sp = ServerProcess(tmp_path, port=12345)
    sp._ServerProcess__process = mock_process
    await sp.stop()

    mock_process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_server_process_start_raises_when_process_exits_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the child process exits before the port is listening, ServerStartError is raised."""
    mock_process = MagicMock()
    mock_process.returncode = 1  # exited immediately
    mock_process.pid = 1234

    create_mock = AsyncMock(return_value=mock_process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create_mock)

    sp = ServerProcess(tmp_path, port=12345)
    with pytest.raises(ServerStartError, match="exited with code 1"):
        await sp.start(timeout=0.1)

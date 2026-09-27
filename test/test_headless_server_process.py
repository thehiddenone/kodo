"""Tests for :class:`kodo.headless.ServerProcess` -- kodo-server subprocess management.

Covers:
* the child environment -- HOME/USERPROFILE/HF_HOME redirection.
* :func:`kodo.headless.pick_free_port` -- returns an int in valid range.
* :class:`ServerProcess` -- properties (``port``, ``ws_url``, ``running``).
* :meth:`ServerProcess.start` / ``.stop`` with a mocked subprocess and a real
  loopback listener standing in for the server's port.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.headless import ServerProcess, ServerStartError, pick_free_port


@pytest.fixture
async def listening_port() -> AsyncIterator[int]:
    """A loopback port something is listening on, as a started server would be."""

    def _on_connect(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def _live_process() -> MagicMock:
    process = MagicMock()
    process.returncode = None
    process.pid = 1234
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.wait = AsyncMock()
    return process


async def _started(
    home: Path,
    port: int,
    process: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    terminate_grace: float = 10.0,
) -> tuple[ServerProcess, AsyncMock]:
    create_mock = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create_mock)
    sp = ServerProcess(home, port=port, terminate_grace=terminate_grace)
    await sp.start(timeout=1.0)
    return sp, create_mock


# ---------------------------------------------------------------------------
# child environment
# ---------------------------------------------------------------------------


async def test_child_env_redirects_home_and_userprofile(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/real/home")
    monkeypatch.setenv("USERPROFILE", "/real/profile")
    _, create_mock = await _started(tmp_path, listening_port, _live_process(), monkeypatch)
    env = create_mock.call_args.kwargs["env"]
    assert env["HOME"] == str(tmp_path.resolve())
    assert env["USERPROFILE"] == str(tmp_path.resolve())


async def test_child_env_keeps_hf_home_from_env(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", "/custom/hf")
    _, create_mock = await _started(tmp_path, listening_port, _live_process(), monkeypatch)
    assert create_mock.call_args.kwargs["env"]["HF_HOME"] == "/custom/hf"


async def test_child_env_defaults_hf_home_to_real_home_cache(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("HOME", "/real/home")
    _, create_mock = await _started(tmp_path, listening_port, _live_process(), monkeypatch)
    expected = str(Path("/real/home") / ".cache" / "huggingface")
    assert create_mock.call_args.kwargs["env"]["HF_HOME"] == expected


async def test_child_env_is_unbuffered_and_preserves_other_vars(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOO", "bar")
    _, create_mock = await _started(tmp_path, listening_port, _live_process(), monkeypatch)
    env = create_mock.call_args.kwargs["env"]
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["FOO"] == "bar"


async def test_start_passes_port_log_level_and_extra_args(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_mock = AsyncMock(return_value=_live_process())
    monkeypatch.setattr("asyncio.create_subprocess_exec", create_mock)
    sp = ServerProcess(
        tmp_path, port=listening_port, log_level="DEBUG", extra_args=("--headless-sandbox", "/w")
    )
    await sp.start(timeout=1.0)
    args = create_mock.call_args.args
    assert args[1:3] == ("-m", "kodo.server")
    assert args[3:5] == ("--port", str(listening_port))
    assert args[5:7] == ("--log-level", "DEBUG")
    assert args[-2:] == ("--headless-sandbox", "/w")


# ---------------------------------------------------------------------------
# pick_free_port
# ---------------------------------------------------------------------------


def test_pick_free_port_returns_int_in_valid_range() -> None:
    port = pick_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_server_process_picks_a_port_when_omitted() -> None:
    assert 1024 <= ServerProcess(Path("/tmp")).port <= 65535


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


async def test_server_process_running_true_after_start(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp, _ = await _started(tmp_path, listening_port, _live_process(), monkeypatch)
    assert sp.running is True


async def test_server_process_running_false_when_child_exited(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _live_process()
    sp, _ = await _started(tmp_path, listening_port, process, monkeypatch)
    process.returncode = 0
    assert sp.running is False


# ---------------------------------------------------------------------------
# ServerProcess -- start / stop
# ---------------------------------------------------------------------------


async def test_server_process_start_raises_if_already_started(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp, _ = await _started(tmp_path, listening_port, _live_process(), monkeypatch)
    with pytest.raises(ServerStartError, match="already started"):
        await sp.start()


async def test_server_process_start_raises_when_process_exits_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the child exits before the port is listening, ServerStartError is raised."""
    process = _live_process()
    process.returncode = 1
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=process))

    sp = ServerProcess(tmp_path, port=pick_free_port())
    with pytest.raises(ServerStartError, match="exited with code 1"):
        await sp.start(timeout=0.1)


async def test_server_process_start_times_out_and_stops_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that never listens is terminated and reported as a start failure."""
    process = _live_process()
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=process))

    sp = ServerProcess(tmp_path, port=pick_free_port())
    with pytest.raises(ServerStartError, match="did not listen"):
        await sp.start(timeout=0.1)
    process.terminate.assert_called()


async def test_server_process_stop_terminates_process(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _live_process()
    sp, _ = await _started(tmp_path, listening_port, process, monkeypatch)
    await sp.stop()
    process.terminate.assert_called()
    process.kill.assert_not_called()


async def test_server_process_stop_noop_when_not_running(tmp_path: Path) -> None:
    sp = ServerProcess(tmp_path)
    # Should not raise.
    await sp.stop()


async def test_server_process_stop_kills_on_timeout(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If SIGTERM isn't honored within the grace period, SIGKILL is sent."""
    process = _live_process()
    waits = 0

    async def _wait() -> None:
        nonlocal waits
        waits += 1
        if waits == 1:
            await asyncio.sleep(10)  # ignores SIGTERM

    process.wait = _wait
    sp, _ = await _started(tmp_path, listening_port, process, monkeypatch, terminate_grace=0.01)
    await sp.stop()

    process.terminate.assert_called()
    process.kill.assert_called()


async def test_server_process_stop_ignores_process_lookup_error(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child already gone makes terminate() raise ProcessLookupError, which is suppressed."""
    process = _live_process()
    process.terminate = MagicMock(side_effect=ProcessLookupError("gone"))
    sp, _ = await _started(tmp_path, listening_port, process, monkeypatch, terminate_grace=0.01)
    await sp.stop()

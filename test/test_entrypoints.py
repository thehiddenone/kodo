"""Behavior tests for the ``python -m`` entry points of Kōdo's sub-packages.

Covers ``kodo.server.__main__`` (``kodo-server``: claim the discovery file,
serve on loopback until a stop is requested, then clean up) and the three
one-line ``__main__`` shims of ``kodo.harbor``, ``kodo.headless`` and
``kodo.llamaserver``, which hand ``sys.argv[1:]`` to the package's ``main`` and
exit with its return code.

``kodo.__main__`` (the ``kodo`` diagnostics CLI) has its own module,
``test_main.py``.

No real signal handler is ever installed and no real ``~/.kodo`` is touched:
``HOME`` points at ``tmp_path``, and the server's :class:`~kodo.server.Lifecycle`
is a subclass whose ``install_signal_handlers`` records the stop callback
instead of registering it with the OS.
"""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from aiohttp import web

import kodo.harbor
import kodo.headless
import kodo.llamaserver
import kodo.server.__main__ as server_main
from kodo.server import Config, Lifecycle

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` — and so ``~/.kodo`` — at an empty ``tmp_path``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _run_as_main(package: str) -> None:
    """``python -m <package>``, in-process."""
    with warnings.catch_warnings():
        # Harmless "found in sys.modules" notice when the shim was imported before.
        warnings.simplefilter("ignore", RuntimeWarning)
        runpy.run_module(package, run_name="__main__")


class _ServeLog:
    """What one ``kodo-server`` run did, as observed from its collaborators."""

    __configs: list[Config]
    __discovery_while_serving: list[dict[str, object]]
    __discovery_during_cleanup: list[dict[str, object]]
    __idle_graces: list[float]
    __signal_callbacks: int
    __cleanups: int
    __shutdown_commits: int

    def __init__(self) -> None:
        """Start with nothing observed."""
        self.__configs = []
        self.__discovery_while_serving = []
        self.__discovery_during_cleanup = []
        self.__idle_graces = []
        self.__signal_callbacks = 0
        self.__cleanups = 0
        self.__shutdown_commits = 0

    @property
    def configs(self) -> tuple[Config, ...]:
        return tuple(self.__configs)

    @property
    def discovery_while_serving(self) -> tuple[dict[str, object], ...]:
        return tuple(self.__discovery_while_serving)

    @property
    def discovery_during_cleanup(self) -> tuple[dict[str, object], ...]:
        return tuple(self.__discovery_during_cleanup)

    @property
    def shutdown_commits(self) -> int:
        return self.__shutdown_commits

    @property
    def idle_graces(self) -> tuple[float, ...]:
        return tuple(self.__idle_graces)

    @property
    def signal_callbacks(self) -> int:
        return self.__signal_callbacks

    @property
    def cleanups(self) -> int:
        return self.__cleanups

    def saw_config(self, config: Config) -> None:
        """Record the configuration the app was built from."""
        self.__configs.append(config)

    def saw_idle_shutdown(self, grace_seconds: float) -> None:
        """Record an idle-reap arming, plus the discovery file's content at that moment."""
        self.__idle_graces.append(grace_seconds)
        discovery = Path.home() / ".kodo" / "kodo-server"
        self.__discovery_while_serving.append(json.loads(discovery.read_text("ascii")))

    def saw_signal_handlers(self) -> None:
        """Record that shutdown-signal handling was requested."""
        self.__signal_callbacks += 1

    def saw_cleanup(self) -> None:
        """Record the app's ``on_shutdown`` firing, plus the discovery file's content then."""
        self.__cleanups += 1
        discovery = Path.home() / ".kodo" / "kodo-server"
        self.__discovery_during_cleanup.append(json.loads(discovery.read_text("ascii")))

    def saw_shutdown_commit(self) -> None:
        """Record the registry being told the shutdown is committed."""
        self.__shutdown_commits += 1


class _FakeConnectionRegistry:
    """Stands in for the real registry's idle self-reap hook."""

    __log: _ServeLog
    __reap_at_once: bool

    def __init__(self, log: _ServeLog, *, reap_at_once: bool) -> None:
        """Bind the run log.

        Args:
            log (_ServeLog): Where observations go.
            reap_at_once (bool): Fire the idle-shutdown callback right away, as
                if the grace period had elapsed with no window connected.
        """
        self.__log = log
        self.__reap_at_once = reap_at_once

    def set_idle_shutdown(self, callback: Callable[[], None], grace_seconds: float) -> None:
        """Record the grace, snapshot the discovery file, and optionally reap now."""
        self.__log.saw_idle_shutdown(grace_seconds)
        if self.__reap_at_once:
            asyncio.get_running_loop().call_soon(callback)

    def begin_shutdown(self) -> None:
        """Record the shutdown commit."""
        self.__log.saw_shutdown_commit()


def _install_server_fakes(
    monkeypatch: pytest.MonkeyPatch, *, reap_at_once: bool, signal_at_once: bool
) -> _ServeLog:
    """Swap in a minimal app and a signal-free :class:`Lifecycle`; return the run log."""
    log = _ServeLog()

    class _NoSignalLifecycle(Lifecycle):
        def install_signal_handlers(self, stop_callback: Callable[[], None]) -> None:
            log.saw_signal_handlers()
            if signal_at_once:
                # As if SIGTERM arrived right after startup.
                asyncio.get_running_loop().call_soon(stop_callback)

    async def _on_shutdown(_app: web.Application) -> None:
        log.saw_cleanup()

    def _create_app(config: Config) -> web.Application:
        log.saw_config(config)
        app = web.Application()
        app[server_main.CONNECTION_REGISTRY_KEY] = _FakeConnectionRegistry(  # type: ignore[assignment]
            log, reap_at_once=reap_at_once
        )
        app.on_shutdown.append(_on_shutdown)
        return app

    monkeypatch.setattr(server_main, "Lifecycle", _NoSignalLifecycle)
    monkeypatch.setattr(server_main, "create_app", _create_app)
    return log


# ---------------------------------------------------------------------------
# kodo.server.__main__
# ---------------------------------------------------------------------------


def test_server_serves_until_the_idle_reap_then_cleans_up(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _install_server_fakes(monkeypatch, reap_at_once=True, signal_at_once=False)

    server_main.main(["--port", "0"])

    assert [c.port for c in log.configs] == [0]
    assert log.discovery_while_serving == ({"pid": os.getpid(), "port": 0, "state": "serving"},)
    assert log.signal_callbacks == 1
    assert len(log.idle_graces) == 1
    assert log.idle_graces[0] > 0
    assert log.shutdown_commits == 1
    assert log.cleanups == 1
    # Teardown runs with the file already saying "stopping", so no launcher
    # attaches to the server while it is going away.
    assert log.discovery_during_cleanup == ({"pid": os.getpid(), "port": 0, "state": "stopping"},)
    assert not (home / ".kodo" / "kodo-server").exists()


def test_server_stops_on_a_shutdown_signal(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _install_server_fakes(monkeypatch, reap_at_once=False, signal_at_once=True)

    server_main.main(["--port", "0"])

    assert log.discovery_while_serving == ({"pid": os.getpid(), "port": 0, "state": "serving"},)
    assert log.shutdown_commits == 1
    assert log.cleanups == 1
    assert log.discovery_during_cleanup == ({"pid": os.getpid(), "port": 0, "state": "stopping"},)
    assert not (home / ".kodo" / "kodo-server").exists()


def test_server_that_fails_to_start_releases_the_discovery_file(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup failure after the claim must not leave a file pointing at a dead server."""

    def _broken_app(_config: Config) -> web.Application:
        raise RuntimeError("app construction failed")

    monkeypatch.setattr(server_main, "create_app", _broken_app)

    with pytest.raises(RuntimeError, match="app construction failed"):
        server_main.main(["--port", "0"])

    assert not (home / ".kodo" / "kodo-server").exists()


def test_server_script_passes_argv_and_swallows_ctrl_c(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m kodo.server`` exits quietly on Ctrl+C instead of printing a traceback."""
    seen: list[list[str] | None] = []

    def _interrupted(cls: type[Config], argv: list[str] | None = None) -> Config:
        seen.append(argv)
        raise KeyboardInterrupt

    monkeypatch.setattr(Config, "from_args", classmethod(_interrupted))
    monkeypatch.setattr(sys, "argv", ["kodo-server", "--port", "0"])

    _run_as_main("kodo.server")

    assert seen == [["--port", "0"]]
    assert not (home / ".kodo" / "kodo-server").exists()


# ---------------------------------------------------------------------------
# The one-line shims: kodo.harbor / kodo.headless / kodo.llamaserver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "package",
    [
        pytest.param(kodo.harbor, id="harbor"),
        pytest.param(kodo.headless, id="headless"),
        pytest.param(kodo.llamaserver, id="llamaserver"),
    ],
)
def test_shim_runs_the_package_main_and_exits_with_its_code(
    package: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def _main(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    monkeypatch.setattr(package, "main", _main)
    monkeypatch.setattr(sys, "argv", ["prog", "--flag", "value"])

    with pytest.raises(SystemExit) as excinfo:
        _run_as_main(package.__name__)

    assert excinfo.value.code == 7
    assert seen == [["--flag", "value"]]

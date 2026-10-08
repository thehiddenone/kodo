"""Entry point for ``python -m kodo.server`` and the ``kodo-server`` CLI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiohttp import web

from ._app import create_app
from ._config import Config
from ._connection_registry import CONNECTION_REGISTRY_KEY
from ._lifecycle import Lifecycle

_log = logging.getLogger(__name__)

# Idle period with zero connected windows before the singleton self-reaps.
_IDLE_SHUTDOWN_SECONDS: float = 5.0


def main(argv: list[str] | None = None) -> None:
    """Parse CLI arguments and run the Kōdo WebSocket server.

    Binds exclusively to ``127.0.0.1`` (loopback) on the configured port.
    Writes a PID file so the VS Code extension can detect stale processes.

    Args:
        argv (list[str] | None): CLI arguments; defaults to ``sys.argv[1:]``.
    """
    config = Config.from_args(argv)

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    lifecycle = Lifecycle(config.port)
    lifecycle.check_and_write()
    try:
        app = create_app(config)
        asyncio.run(_serve(app, config, lifecycle))
    finally:
        # Normally already gone (``_serve`` removes it); this covers a startup
        # that failed before serving, e.g. the port being taken.
        lifecycle.remove()


async def _serve(app: web.Application, config: Config, lifecycle: Lifecycle) -> None:
    """Async entry point: start the HTTP server and wait for a shutdown signal.

    Args:
        app (web.Application): Configured aiohttp application.
        config (Config): Resolved server configuration.
        lifecycle (Lifecycle): PID-file and signal-handler manager.
    """
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="127.0.0.1", port=config.port)
    await site.start()
    lifecycle.mark_serving()
    _log.info("Listening on ws://127.0.0.1:%d/ws", config.port)

    registry = app[CONNECTION_REGISTRY_KEY]
    stop_event = asyncio.Event()

    def _stop() -> None:
        # The commit point of every shutdown path (idle self-reap,
        # `server.shutdown`, SIGTERM/SIGINT). Refuse new windows and advertise
        # "stopping" before teardown starts: teardown takes a while (both
        # llama-servers and every engine stop), and a launcher reading the
        # discovery file meanwhile must wait for this process to exit rather
        # than attach to a server that is about to vanish.
        registry.begin_shutdown()
        lifecycle.mark_stopping()
        stop_event.set()

    lifecycle.install_signal_handlers(_stop)

    # Singleton self-reap: shut down once no window has been connected for the
    # idle grace period (the launcher relaunches on the next window).
    registry.set_idle_shutdown(_stop, _IDLE_SHUTDOWN_SECONDS)

    try:
        await stop_event.wait()
    finally:
        _log.info("Shutting down…")
        await runner.cleanup()
        lifecycle.remove()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main(sys.argv[1:])

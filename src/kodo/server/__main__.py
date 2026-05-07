"""Entry point for ``python -m kodo.server`` and the ``kodo-server`` CLI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiohttp import web

from ._app import create_app
from ._config import Config
from ._lifecycle import Lifecycle

_log = logging.getLogger(__name__)


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

    lifecycle = Lifecycle(config.project)
    lifecycle.check_and_write_pid()

    app = create_app(config)

    asyncio.run(_serve(app, config, lifecycle))


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
    _log.info("Listening on ws://127.0.0.1:%d/ws", config.port)

    stop_event = asyncio.Event()
    lifecycle.install_signal_handlers(stop_event.set)

    try:
        await stop_event.wait()
    finally:
        _log.info("Shutting down…")
        await runner.cleanup()
        lifecycle.remove_pid()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main(sys.argv[1:])

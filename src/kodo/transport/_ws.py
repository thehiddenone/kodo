"""aiohttp WebSocket handler and message dispatcher for the Kōdo wire protocol."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine

from aiohttp import WSMsgType, web

from ._envelope import Envelope
from ._outbox import Outbox

_log = logging.getLogger(__name__)

# Async handler: (app_state, envelope) -> None
HandlerFn = Callable[["AppState", Envelope], Coroutine[None, None, None]]

# Typed app-level key — avoids NotAppKeyWarning from aiohttp
APP_STATE_KEY: web.AppKey[AppState] = web.AppKey("state")


class AppState:
    """Mutable server-side state attached to the aiohttp application.

    A single instance lives on ``app['state']`` for the lifetime of the server.
    """

    __outbox: Outbox
    __ws: web.WebSocketResponse | None
    __handlers: dict[str, HandlerFn]

    def __init__(self, outbox: Outbox) -> None:
        """Initialise app state with an empty handler registry.

        Args:
            outbox (Outbox): Shared disconnect-tolerant send queue.
        """
        self.__outbox = outbox
        self.__ws = None
        self.__handlers = {}

    @property
    def outbox(self) -> Outbox:
        """The shared outbound message queue."""
        return self.__outbox

    @property
    def ws(self) -> web.WebSocketResponse | None:
        """Currently active WebSocket connection, or ``None``."""
        return self.__ws

    def register_handler(self, msg_type: str, fn: HandlerFn) -> None:
        """Register a handler for a specific message type.

        Args:
            msg_type (str): The ``payload["type"]`` string to match.
            fn (HandlerFn): Async handler invoked when a matching frame arrives.
        """
        self.__handlers[msg_type] = fn

    async def send(self, env: Envelope) -> None:
        """Deliver an envelope immediately or buffer it if disconnected.

        Args:
            env (Envelope): The envelope to send.
        """
        await self.__outbox.send_or_buffer(env, self.__ws)

    async def run_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Accept a WebSocket upgrade and process frames until disconnected.

        Only one connection is accepted at a time.  If a previous connection
        is still open it is closed before the new one is accepted.

        Args:
            request (web.Request): Incoming HTTP upgrade request.

        Returns:
            web.WebSocketResponse: The completed WS response.
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        if self.__ws is not None:
            _log.warning("Replacing existing WebSocket connection")
            await self.__ws.close()

        self.__ws = ws
        _log.info("WebSocket connected from %s", request.remote)

        await self.__outbox.drain_to(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.__dispatch(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    _log.error("WebSocket protocol error: %s", ws.exception())
        finally:
            self.__ws = None
            _log.info("WebSocket disconnected")

        return ws

    async def __dispatch(self, raw: str) -> None:
        try:
            env = Envelope.from_json(raw)
        except (KeyError, ValueError) as exc:
            _log.warning("Malformed frame: %s", exc)
            return

        msg_type = str(env.payload.get("type", ""))
        handler = self.__handlers.get(msg_type)
        if handler is not None:
            await handler(self, env)
        else:
            _log.warning("Unhandled message type %r (id=%s)", msg_type, env.id)
            err = Envelope.make_response(
                env.id,
                {
                    "type": "error",
                    "code": "unknown_message",
                    "message": f"Unknown message type: {msg_type!r}",
                    "recoverable": True,
                },
            )
            await self.send(err)


def get_state(app: web.Application) -> AppState:
    """Retrieve the :class:`AppState` from an aiohttp application.

    Args:
        app (web.Application): The running aiohttp application.

    Returns:
        AppState: The attached application state.
    """
    return app[APP_STATE_KEY]

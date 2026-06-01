"""aiohttp WebSocket handler and message dispatcher for the Kōdo wire protocol."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine

from aiohttp import WSMsgType, web

from kodo.common import Envelope

from ._outbox import Outbox

_log = logging.getLogger(__name__)

# Async handler: (app_state, envelope) -> None
HandlerFn = Callable[["WebSocketDispatcher", Envelope], Coroutine[None, None, None]]

# Typed app-level key — avoids NotAppKeyWarning from aiohttp
APP_STATE_KEY: web.AppKey[WebSocketDispatcher] = web.AppKey("state")


class WebSocketDispatcher:
    """Mutable server-side state attached to the aiohttp application.

    A single instance lives on ``app['state']`` for the lifetime of the server.

    Two dispatch paths coexist (WS_PROTOCOL.md §2.1):

    - ``kind=request`` frames from the client are dispatched by
      ``payload["type"]`` to registered :data:`HandlerFn` callables.
    - ``kind=response`` frames from the client are resolved by
      ``correlation_id`` against futures registered via
      :meth:`register_response_future`.  These represent the client
      answering a server-initiated ``kind=request`` (e.g. a
      ``prompt.approval`` or ``prompt.question`` gate).
    """

    __outbox: Outbox
    __ws: web.WebSocketResponse | None
    __handlers: dict[str, HandlerFn]
    __pending_responses: dict[str, asyncio.Future[dict[str, object]]]

    def __init__(self, outbox: Outbox) -> None:
        """Initialise app state with an empty handler and response registry.

        Args:
            outbox (Outbox): Shared disconnect-tolerant send queue.
        """
        self.__outbox = outbox
        self.__ws = None
        self.__handlers = {}
        self.__pending_responses = {}

    @property
    def outbox(self) -> Outbox:
        """The shared outbound message queue."""
        return self.__outbox

    @property
    def ws(self) -> web.WebSocketResponse | None:
        """Currently active WebSocket connection, or ``None``."""
        return self.__ws

    def register_handler(self, msg_type: str, fn: HandlerFn) -> None:
        """Register a handler for a specific client-request message type.

        Args:
            msg_type (str): The ``payload["type"]`` string to match on
                incoming ``kind=request`` frames.
            fn (HandlerFn): Async handler invoked when a matching frame
                arrives.
        """
        self.__handlers[msg_type] = fn

    def register_response_future(
        self,
        request_id: str,
        future: asyncio.Future[dict[str, object]],
    ) -> None:
        """Register a future to be resolved when the client responds to a
        server-initiated ``kind=request`` frame.

        The future is resolved with the response ``payload`` dict when the
        client sends a ``kind=response`` whose ``correlation_id`` equals
        ``request_id``.

        Args:
            request_id (str): The ``id`` of the server-sent request envelope.
            future (asyncio.Future): Future to resolve with the response
                payload.
        """
        self.__pending_responses[request_id] = future

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
            # Cancel all pending server-initiated request futures so that
            # callers (KeyBroker, GateOrchestrator) are not left hanging.
            for future in self.__pending_responses.values():
                if not future.done():
                    future.cancel()
            self.__pending_responses.clear()
            _log.info("WebSocket disconnected")

        return ws

    async def __dispatch(self, raw: str) -> None:
        try:
            env = Envelope.from_json(raw)
        except (KeyError, ValueError) as exc:
            _log.warning("Malformed frame: %s", exc)
            return

        # kind=response: resolve a pending server-initiated request future.
        # These are never themselves replied to — silently drop if no match.
        if env.kind == "response":
            if env.correlation_id:
                future = self.__pending_responses.pop(env.correlation_id, None)
                if future is not None and not future.done():
                    future.set_result(env.payload)
                else:
                    _log.debug(
                        "kind=response with no pending future (correlation_id=%s)",
                        env.correlation_id,
                    )
            return

        # kind=request (and others): dispatch by payload["type"]
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


def get_state(app: web.Application) -> WebSocketDispatcher:
    """Retrieve the :class:`AppState` from an aiohttp application.

    Args:
        app (web.Application): The running aiohttp application.

    Returns:
        AppState: The attached application state.
    """
    return app[APP_STATE_KEY]

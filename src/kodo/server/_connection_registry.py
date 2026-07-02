"""ConnectionRegistry — accepts many WebSocket connections and routes frames.

This is the server-tier glue between the transport primitives and the
:class:`SessionManager`.  It depends on the manager (one-way); the manager never
depends on it.  Each inbound frame is routed by ``payload.type`` to a registered
handler; the session it targets is resolved from ``payload.session_id`` (every
frame except ``hello`` must carry one).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import WSMsgType, web

from kodo.common import Envelope
from kodo.transport import Connection

from ._session import Session
from ._session_manager import SessionManager

__all__ = ["ConnectionRegistry", "Request", "HandlerFn", "CONNECTION_REGISTRY_KEY"]

_log = logging.getLogger(__name__)


@dataclass
class Request:
    """One inbound client frame plus the context a handler needs to act on it.

    Attributes:
        manager: The session manager (for create/open/list/release).
        connection: The connection the frame arrived on.
        env: The decoded envelope.
        session: The session resolved from ``payload.session_id`` (``None`` for
            ``hello`` and any frame whose session is unknown).
    """

    manager: SessionManager
    connection: Connection
    env: Envelope
    session: Session | None

    @property
    def session_id(self) -> str:
        """The ``session_id`` carried in the payload (``""`` if absent)."""
        return str(self.env.payload.get("session_id", ""))

    async def reply(self, payload: dict[str, object]) -> None:
        """Send a response correlated to this request."""
        await self.connection.send(Envelope.make_response(self.env.id, payload))


HandlerFn = Callable[[Request], Awaitable[None]]

# Imported lazily for the type alias above without a runtime cycle.

CONNECTION_REGISTRY_KEY: web.AppKey[ConnectionRegistry] = web.AppKey("connection_registry")


class ConnectionRegistry:
    """Accepts N concurrent WebSocket connections and dispatches their frames."""

    def __init__(self, manager: SessionManager) -> None:
        self.__manager = manager
        self.__handlers: dict[str, HandlerFn] = {}
        self.__active = 0
        self.__idle_cb: Callable[[], None] | None = None
        self.__idle_grace = 0.0
        self.__idle_timer: asyncio.TimerHandle | None = None

    @property
    def manager(self) -> SessionManager:
        """The session manager this registry routes to."""
        return self.__manager

    def register_handler(self, msg_type: str, fn: HandlerFn) -> None:
        """Register a handler for a client-request ``payload.type``."""
        self.__handlers[msg_type] = fn

    def set_idle_shutdown(self, callback: Callable[[], None], grace_seconds: float) -> None:
        """Self-reap when no window is connected for *grace_seconds*.

        The singleton stays alive while ≥1 window is connected; once the last
        one leaves (and no new one arrives within the grace) *callback* is
        invoked to stop the server.  Armed immediately so a server nobody ever
        connects to also eventually exits.

        Args:
            callback: Invoked to trigger graceful shutdown (e.g. ``stop_event.set``).
            grace_seconds: Idle period before shutdown.
        """
        self.__idle_cb = callback
        self.__idle_grace = grace_seconds
        self.__arm_idle()

    async def run_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Accept one WebSocket upgrade and process its frames until it closes."""
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        conn = Connection(ws)
        self.__active += 1
        self.__cancel_idle()
        _log.info("WebSocket connected from %s (conn=%s)", request.remote, conn.id[:8])

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.__dispatch(conn, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    _log.error("WebSocket protocol error: %s", ws.exception())
        finally:
            conn.cancel_pending()
            self.__manager.drop_connection(conn)
            self.__active -= 1
            if self.__active <= 0:
                self.__arm_idle()
            _log.info("WebSocket disconnected (conn=%s)", conn.id[:8])

        return ws

    def __arm_idle(self) -> None:
        self.__cancel_idle()
        if self.__idle_cb is None:
            return
        loop = asyncio.get_event_loop()
        self.__idle_timer = loop.call_later(self.__idle_grace, self.__maybe_shutdown)

    def __cancel_idle(self) -> None:
        if self.__idle_timer is not None:
            self.__idle_timer.cancel()
            self.__idle_timer = None

    def __maybe_shutdown(self) -> None:
        self.__idle_timer = None
        if self.__active > 0 or self.__idle_cb is None:
            return
        if self.__manager.any_running():
            # A turn is still streaming (e.g. every window is mid-reload).
            # Reaping now would destroy work a reconnecting window is about to
            # resume — defer for another grace period and check again.
            _log.info(
                "No clients for %.0fs but a turn is still running — deferring self-reap",
                self.__idle_grace,
            )
            self.__arm_idle()
            return
        _log.info("No clients for %.0fs — self-reaping singleton server", self.__idle_grace)
        self.__idle_cb()

    async def __dispatch(self, conn: Connection, raw: str) -> None:
        try:
            env = Envelope.from_json(raw)
        except (KeyError, ValueError) as exc:
            _log.warning("Malformed frame: %s", exc)
            return

        if env.kind == "response":
            if env.correlation_id:
                conn.resolve_response(env.correlation_id, env.payload)
            return

        msg_type = str(env.payload.get("type", ""))
        handler = self.__handlers.get(msg_type)
        if handler is None:
            await conn.send(
                Envelope.make_response(
                    env.id,
                    {
                        "type": "error",
                        "code": "unknown_message",
                        "message": f"Unknown message type: {msg_type!r}",
                        "recoverable": True,
                    },
                )
            )
            return

        session_id = str(env.payload.get("session_id", ""))
        session = self.__manager.get(session_id) if session_id else None
        await handler(Request(self.__manager, conn, env, session))

"""Per-connection and per-session transport primitives.

The singleton server holds many live :class:`Connection` objects (one per VS
Code window) and one :class:`SessionChannel` per session.  A session outlives
its socket: while the window is briefly disconnected (reload), the channel
buffers outbound events in its :class:`Outbox` and replays them when the window
reconnects.

These are pure transport types — they know nothing about sessions or the LLM
gateway.  The :class:`kodo.server.ConnectionRegistry` wires them together.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from aiohttp import web

from kodo.common import Envelope

from ._outbox import Outbox

__all__ = ["Connection", "SessionChannel"]

_log = logging.getLogger(__name__)


class Connection:
    """One live WebSocket to a single VS Code window.

    Owns the futures for server-initiated requests (key/gate prompts) issued
    over *this* socket, so a disconnect cancels exactly those and no others.
    """

    __ws: web.WebSocketResponse
    __id: str
    __pending: dict[str, asyncio.Future[dict[str, object]]]

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.__ws = ws
        self.__id = uuid.uuid4().hex
        self.__pending = {}

    @property
    def id(self) -> str:
        """Opaque connection identifier (used as the session ownership token)."""
        return self.__id

    @property
    def ws(self) -> web.WebSocketResponse:
        """The underlying aiohttp WebSocket response."""
        return self.__ws

    @property
    def closed(self) -> bool:
        """Whether the socket is closed."""
        return self.__ws.closed

    async def send(self, env: Envelope) -> None:
        """Send an envelope on this socket (best-effort)."""
        if not self.__ws.closed:
            await self.__ws.send_str(env.to_json())

    def register_response_future(
        self, request_id: str, future: asyncio.Future[dict[str, object]]
    ) -> None:
        """Register a future resolved when the client answers ``request_id``."""
        self.__pending[request_id] = future

    def resolve_response(self, correlation_id: str, payload: dict[str, object]) -> None:
        """Resolve a pending server-initiated request by its correlation id."""
        future = self.__pending.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(payload)
        elif future is None:
            _log.debug("kind=response with no pending future (correlation_id=%s)", correlation_id)

    def cancel_pending(self) -> None:
        """Cancel all outstanding server-initiated request futures (on disconnect)."""
        for future in self.__pending.values():
            if not future.done():
                future.cancel()
        self.__pending.clear()


class SessionChannel:
    """A session's stable sink — survives the window reconnecting.

    Implements :class:`kodo.common.MessageSink` and
    :class:`kodo.common.ResponseChannel`.  When a connection is attached, frames
    go straight to it; while detached, frames buffer in the :class:`Outbox` and
    replay on the next attach.
    """

    __outbox: Outbox
    __conn: Connection | None

    def __init__(self, outbox: Outbox | None = None) -> None:
        self.__outbox = outbox or Outbox()
        self.__conn = None

    @property
    def outbox(self) -> Outbox:
        """The session's disconnect-tolerant outbound buffer."""
        return self.__outbox

    @property
    def connection(self) -> Connection | None:
        """The currently attached connection, or ``None`` while disconnected."""
        return self.__conn

    async def attach(self, conn: Connection) -> None:
        """Bind a live connection and replay any buffered frames to it."""
        self.__conn = conn
        await self.__outbox.drain_to(conn.ws)

    def detach(self, conn: Connection | None = None) -> None:
        """Unbind the connection (buffer subsequent frames).

        Args:
            conn: If given, only detach when it matches the current connection
                (avoids a late disconnect clobbering a freshly attached one).
        """
        if conn is None or self.__conn is conn:
            self.__conn = None

    async def send(self, env: Envelope) -> None:
        """Send to the attached connection, else buffer for replay."""
        conn = self.__conn
        if conn is not None and not conn.closed:
            await conn.send(env)
        else:
            await self.__outbox.enqueue(env)

    def register_response_future(
        self, request_id: str, future: asyncio.Future[dict[str, object]]
    ) -> None:
        """Delegate a server-initiated request future to the live connection.

        Key/gate prompts only run during an active (connected) turn; if the
        window is gone the future stays unresolved until the turn is cancelled.
        """
        if self.__conn is not None:
            self.__conn.register_response_future(request_id, future)

"""Per-connection and per-session transport primitives.

The singleton server holds many live :class:`Connection` objects (one per VS
Code window) and one :class:`SessionChannel` per session.  A session outlives
its socket: while the window is briefly disconnected (reload), the channel
buffers outbound events in its :class:`Outbox` and replays them when the window
reconnects.  A server-initiated request/response round-trip (an approval,
question, permission, or API-key prompt) also outlives the socket: its future
and its request envelope live on the :class:`SessionChannel`, not the
:class:`Connection` that happened to be attached when it was sent, so a
disconnect never cancels one still outstanding — see
:meth:`SessionChannel.register_response_future` /
:meth:`SessionChannel.replay_pending_requests`.

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

    Deliberately owns nothing session-scoped: a server-initiated request
    (key/gate prompt) is tracked by the session's own :class:`SessionChannel`
    instead (see its docstring), specifically so a socket disconnect —
    e.g. a VS Code window reload — never cancels one that is still
    outstanding. A ``Connection`` is only ever the transient thing a
    ``SessionChannel`` happens to be attached to at a given moment.
    """

    __ws: web.WebSocketResponse
    __id: str

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.__ws = ws
        self.__id = uuid.uuid4().hex

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


class SessionChannel:
    """A session's stable sink — survives the window reconnecting.

    Implements :class:`kodo.common.MessageSink` and
    :class:`kodo.common.ResponseChannel`.  When a connection is attached, frames
    go straight to it; while detached, frames buffer in the :class:`Outbox` and
    replay on the next attach.
    """

    __outbox: Outbox
    __conn: Connection | None
    __pending_futures: dict[str, asyncio.Future[dict[str, object]]]
    __pending_requests: dict[str, Envelope]

    def __init__(self, outbox: Outbox | None = None) -> None:
        self.__outbox = outbox or Outbox()
        self.__conn = None
        self.__pending_futures = {}
        self.__pending_requests = {}

    @property
    def outbox(self) -> Outbox:
        """The session's disconnect-tolerant outbound buffer."""
        return self.__outbox

    @property
    def connection(self) -> Connection | None:
        """The currently attached connection, or ``None`` while disconnected."""
        return self.__conn

    async def attach(self, conn: Connection) -> None:
        """Bind a live connection. Does not replay the backlog.

        Callers must send the reconnect "base layer" (hello.ack, state,
        session.history, …) via :meth:`send` first, then call
        :meth:`replay_backlog` — never the other way around. Otherwise a
        buffered mid-turn frame (e.g. a stray ``tool_call``) can reach the
        webview before ``session.history`` does, tripping the reducer's
        "history already applied" guard and silently dropping the scrollback
        (see kodo-vsix reducer.ts ``session_history``).
        """
        self.__conn = conn

    async def replay_backlog(self) -> None:
        """Flush any frames buffered while disconnected to the live connection.

        Must be called only after the reconnect base layer has already been
        sent on this connection (see :meth:`attach`).
        """
        if self.__conn is not None:
            await self.__outbox.drain_to(self.__conn.ws)

    def detach(self, conn: Connection | None = None) -> None:
        """Unbind the connection (buffer subsequent frames).

        Args:
            conn: If given, only detach when it matches the current connection
                (avoids a late disconnect clobbering a freshly attached one).
        """
        if conn is None or self.__conn is conn:
            self.__conn = None

    async def send(self, env: Envelope) -> None:
        """Send to the attached connection, else buffer for replay.

        A ``kind="request"`` envelope is additionally remembered (keyed by
        ``env.id``) until :meth:`resolve_response` pops it, so
        :meth:`replay_pending_requests` can re-send it verbatim to a freshly
        reconnected window — this is what lets a still-outstanding
        approval/question/permission/key prompt re-render in a webview that
        has no memory of it (e.g. after the extension host itself restarted),
        on top of whatever the disconnect-buffered :class:`Outbox` replays.
        """
        if env.kind == "request":
            self.__pending_requests[env.id] = env
        conn = self.__conn
        if conn is not None and not conn.closed:
            await conn.send(env)
        else:
            await self.__outbox.enqueue(env)

    def register_response_future(
        self, request_id: str, future: asyncio.Future[dict[str, object]]
    ) -> None:
        """Register a future resolved when the client answers *request_id*.

        Session-scoped, not tied to whichever :class:`Connection` happens to
        be attached right now: a server-initiated prompt (approval, question,
        permission, API key) survives the window disconnecting and
        reconnecting on a brand-new socket. Only the session's own worker
        task being cancelled — genuine teardown (session delete, server
        shutdown), never a transient socket drop — actually ends the wait,
        by delivering ``CancelledError`` at the ``await`` inside the gate
        that registered it.
        """
        self.__pending_futures[request_id] = future

    def resolve_response(self, correlation_id: str, payload: dict[str, object]) -> None:
        """Resolve a pending server-initiated request by its correlation id."""
        future = self.__pending_futures.pop(correlation_id, None)
        self.__pending_requests.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(payload)
        elif future is None:
            _log.debug("kind=response with no pending future (correlation_id=%s)", correlation_id)

    async def replay_pending_requests(self) -> None:
        """Re-send every still-unanswered server-initiated request.

        Called after a reconnect's base layer (hello.ack/state/session.history)
        and backlog have gone out, so a freshly attached window — including
        one with no in-memory record of the prompt at all, e.g. because the
        extension host itself restarted — re-renders any outstanding
        approval/question/permission/key panel exactly as first shown, with
        the same request id, so the eventual answer still resolves the
        original waiting future.
        """
        if self.__conn is None:
            return
        for env in list(self.__pending_requests.values()):
            await self.__conn.send(env)

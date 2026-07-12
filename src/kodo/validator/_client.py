"""WebSocket client that plays the VS Code extension's role for a session.

Speaks the exact wire contract from ``doc/WS_PROTOCOL.md``: ``hello``
handshake, client→server requests matched by ``correlation_id``, and — the
part a plain test client lacks — the **server→client** requests
(``prompt.question`` / ``prompt.approval`` / ``prompt.permission`` /
``api_key.request``), which are routed to a :class:`~kodo.validator._user.
UserSimulator` and answered on the wire, with every exchange logged to the
:class:`~kodo.validator._transcript.Transcript`.

Turn-end detection: a turn is considered over once the phase was seen
``running`` and has settled back to a resting phase (``awaiting_user`` /
``done`` / ``stopped`` / ``error``) with no simulated interaction still in
flight. Because "resting" is also the phase *while* a question is pending,
the check must hold through a short settle window before it counts — the
engine flips back to ``running`` almost immediately after an answer lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import cast

import aiohttp

from kodo.common import Envelope
from kodo.transport import (
    MSG_HELLO,
    SREQ_API_KEY_REQUEST,
    SREQ_PROMPT_APPROVAL,
    SREQ_PROMPT_PERMISSION,
    SREQ_PROMPT_QUESTION,
)

from ._transcript import Transcript
from ._user import UserSimulator

__all__ = ["ProtocolError", "ValidatorClient"]

_log = logging.getLogger(__name__)

_RESTING_PHASES = frozenset({"intake", "awaiting_user", "done", "stopped", "error"})
_DEFAULT_REQUEST_TIMEOUT = 30.0


class ProtocolError(RuntimeError):
    """The server answered a request with an error payload."""


class ValidatorClient:
    """One WebSocket connection driving one kodo session.

    Args:
        url: The server's ``ws://…/ws`` endpoint.
        transcript: Recorder receiving every frame and interaction.
        user: Policy answering interactive server requests.
        window_id: Stable pseudo-window identifier for session ownership.
    """

    def __init__(
        self,
        url: str,
        transcript: Transcript,
        user: UserSimulator,
        *,
        window_id: str = "kodo-validator",
    ) -> None:
        self.__url = url
        self.__transcript = transcript
        self.__user = user
        self.__window_id = window_id

        self.__http: aiohttp.ClientSession | None = None
        self.__ws: aiohttp.ClientWebSocketResponse | None = None
        self.__recv_task: asyncio.Task[None] | None = None
        self.__pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.__responder_tasks: set[asyncio.Task[None]] = set()
        self.__streams: dict[str, dict[str, object]] = {}

        self.__session_id: str | None = None
        self.__phase: str | None = None
        self.__saw_running = False
        self.__pending_server_requests = 0
        self.__closed = False
        self.__changed = asyncio.Condition()

    @property
    def session_id(self) -> str | None:
        """The session bound by ``hello``, once the handshake completed."""
        return self.__session_id

    @property
    def phase(self) -> str | None:
        """Latest ``state.phase`` seen on the wire."""
        return self.__phase

    @property
    def connected(self) -> bool:
        """True while the WebSocket is open."""
        return self.__ws is not None and not self.__ws.closed and not self.__closed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket and start the receive pump."""
        self.__http = aiohttp.ClientSession()
        self.__ws = await self.__http.ws_connect(self.__url)
        self.__recv_task = asyncio.create_task(self.__recv_loop(), name="validator-recv")
        self.__transcript.record("note", "lifecycle", {"event": "connected", "url": self.__url})

    async def hello(
        self, *, session_id: str | None = None, thinking_level: str | None = None
    ) -> dict[str, object]:
        """Perform the ``hello`` handshake and bind (or resume) a session.

        Args:
            session_id (str | None): Session to resume; a new one when None.
            thinking_level (str | None): For a brand-new session only, a
                valid tier slug for the currently active local model's
                thinking family to seed ``state.thinking_level`` with
                instead of the family default (WS_PROTOCOL.md §4.1) — used
                by the RVP judge session (doc/VALIDATOR.md §9) to pin its
                tier once its session actually exists. Ignored server-side
                when *session_id* resumes an existing session.

        Returns:
            dict[str, object]: The ``hello.ack`` payload.

        Raises:
            ProtocolError: On a handshake error (e.g. ``session_in_use``).
        """
        payload: dict[str, object] = {
            "client": "kodo-validator",
            "version": "1",
            "window_id": self.__window_id,
        }
        if session_id is not None:
            payload["session_id"] = session_id
        if thinking_level is not None:
            payload["thinking_level"] = thinking_level
        ack = await self.request(MSG_HELLO, payload, session_scoped=False)
        self.__session_id = str(ack["session_id"])
        state = ack.get("state")
        if isinstance(state, dict):
            self.__apply_state(cast(dict[str, object], state))
        return ack

    async def close(self) -> None:
        """Stop the pump and close the connection (idempotent)."""
        self.__closed = True
        for task in list(self.__responder_tasks):
            task.cancel()
        if self.__recv_task is not None:
            self.__recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.__recv_task
            self.__recv_task = None
        if self.__ws is not None:
            with contextlib.suppress(Exception):
                await self.__ws.close()
            self.__ws = None
        if self.__http is not None:
            await self.__http.close()
            self.__http = None
        self.__fail_pending(ConnectionError("Client closed"))

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def request(
        self,
        msg_type: str,
        payload: dict[str, object] | None = None,
        *,
        session_scoped: bool = True,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        check: bool = True,
        **fields: object,
    ) -> dict[str, object]:
        """Send one client→server request and await its response payload.

        Args:
            msg_type (str): The ``payload.type`` (an ``MSG_*`` constant).
            payload (dict[str, object] | None): Message fields as a mapping.
            session_scoped (bool): Attach the bound ``session_id`` (every
                message except ``hello``).
            timeout (float): Seconds to wait for the response.
            check (bool): Raise :class:`ProtocolError` on an error payload.
            **fields: Message-specific fields (merged over *payload*).

        Returns:
            dict[str, object]: The response payload.

        Raises:
            ProtocolError: If *check* and the response carries an error.
            ConnectionError: If the socket is not open.
            TimeoutError: If no response arrives within *timeout*.
        """
        ws = self.__ws
        if ws is None or ws.closed or self.__closed:
            raise ConnectionError("WebSocket is not connected")
        body: dict[str, object] = {"type": msg_type, **(payload or {}), **fields}
        if session_scoped:
            if self.__session_id is None:
                raise ConnectionError("No session bound; call hello() first")
            body["session_id"] = self.__session_id
        env = Envelope(kind="request", payload=body)
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.__pending[env.id] = future
        self.__transcript.record("send", "request", body, correlation_id=env.id)
        await ws.send_str(env.to_json())
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.__pending.pop(env.id, None)
        if check and (response.get("error") or response.get("type") == "error"):
            raise ProtocolError(f"{msg_type} failed: {response}")
        return response

    async def send(
        self,
        msg_type: str,
        payload: dict[str, object] | None = None,
        *,
        session_scoped: bool = False,
        **fields: object,
    ) -> None:
        """Send one client→server frame that expects no correlated response.

        Some server handlers (e.g. ``local_llm.install``/``.resume``/``.pause``,
        WS_PROTOCOL.md §7.6) only ever push ``event`` frames back
        (§5.12a ``local_llm.registry_state``) and never a ``response`` —
        :meth:`request` would block until its timeout waiting for one that
        never arrives. Use this instead for those message types.

        Args:
            msg_type (str): The ``payload.type`` (an ``MSG_*`` constant).
            payload (dict[str, object] | None): Message fields as a mapping.
            session_scoped (bool): Attach the bound ``session_id``; most
                ``local_llm.*`` commands operate on the process-wide registry
                and need no session.
            **fields: Message-specific fields (merged over *payload*).

        Raises:
            ConnectionError: If the socket is not open (or session-scoped
                with no session bound yet).
        """
        ws = self.__ws
        if ws is None or ws.closed or self.__closed:
            raise ConnectionError("WebSocket is not connected")
        body: dict[str, object] = {"type": msg_type, **(payload or {}), **fields}
        if session_scoped:
            if self.__session_id is None:
                raise ConnectionError("No session bound; call hello() first")
            body["session_id"] = self.__session_id
        env = Envelope(kind="request", payload=body)
        self.__transcript.record("send", "request", body, correlation_id=env.id)
        await ws.send_str(env.to_json())

    # ------------------------------------------------------------------
    # Turn tracking
    # ------------------------------------------------------------------

    def begin_turn(self) -> None:
        """Reset turn tracking; call right before submitting a prompt."""
        self.__saw_running = False

    async def wait_turn_end(self, *, timeout: float = 900.0, settle_seconds: float = 2.0) -> str:
        """Block until the in-flight turn has finished.

        A turn is finished once the phase has been seen ``running`` since
        :meth:`begin_turn` and now rests (``awaiting_user`` / ``done`` /
        ``stopped`` / ``error``) with no simulated interaction in flight,
        stable across *settle_seconds* (see module docstring for why the
        settle window is needed).

        Args:
            timeout (float): Overall seconds before giving up.
            settle_seconds (float): How long the resting condition must hold.

        Returns:
            str: The final resting phase.

        Raises:
            TimeoutError: If the turn does not finish within *timeout*.
            ConnectionError: If the connection drops while waiting.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Turn did not finish within {timeout}s (phase={self.__phase!r})"
                )
            async with self.__changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self.__changed.wait_for(self.__turn_ended), timeout=remaining
                    )
            if self.__closed:
                raise ConnectionError("Connection closed while waiting for turn end")
            if not self.__turn_ended():
                continue
            await asyncio.sleep(settle_seconds)
            if self.__turn_ended():
                phase = self.__phase
                assert phase is not None
                return phase

    def __turn_ended(self) -> bool:
        if self.__closed:
            return True  # wake the waiter; wait_turn_end re-checks and raises
        return (
            self.__saw_running
            and self.__phase in _RESTING_PHASES
            and self.__pending_server_requests == 0
            and not self.__responder_tasks
        )

    # ------------------------------------------------------------------
    # Receive pump
    # ------------------------------------------------------------------

    async def __recv_loop(self) -> None:
        ws = self.__ws
        assert ws is not None
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    env = Envelope.from_json(str(msg.data))
                except (KeyError, ValueError):
                    _log.exception("Undecodable frame: %r", msg.data)
                    continue
                await self.__dispatch(env)
        except Exception:
            _log.exception("Receive pump failed")
        finally:
            self.__closed = True
            self.__transcript.record("note", "lifecycle", {"event": "disconnected"})
            self.__fail_pending(ConnectionError("Connection closed"))
            async with self.__changed:
                self.__changed.notify_all()

    async def __dispatch(self, env: Envelope) -> None:
        self.__transcript.record("recv", env.kind, env.payload, correlation_id=env.correlation_id)
        if env.kind == "response":
            self.__resolve_response(env)
        elif env.kind == "event":
            await self.__on_event(env.payload)
        elif env.kind in ("stream_chunk", "thinking_chunk", "toolgen_chunk"):
            self.__on_chunk(env)
        elif env.kind == "stream_end":
            self.__on_stream_end(env)
        elif env.kind == "request":
            self.__on_server_request(env)

    def __resolve_response(self, env: Envelope) -> None:
        if env.correlation_id is None:
            return
        future = self.__pending.get(env.correlation_id)
        if future is not None and not future.done():
            future.set_result(env.payload)

    async def __on_event(self, payload: dict[str, object]) -> None:
        if payload.get("type") == "state":
            self.__apply_state(payload)
        async with self.__changed:
            self.__changed.notify_all()

    def __apply_state(self, state: dict[str, object]) -> None:
        phase = state.get("phase")
        if isinstance(phase, str):
            self.__phase = phase
            if phase == "running":
                self.__saw_running = True

    def __on_chunk(self, env: Envelope) -> None:
        if env.correlation_id is None:
            return
        stream = self.__streams.setdefault(
            env.correlation_id,
            {"stream": str(env.payload.get("type", env.kind)), "parts": []},
        )
        cast(list[str], stream["parts"]).append(str(env.payload.get("text", "")))

    def __on_stream_end(self, env: Envelope) -> None:
        if env.correlation_id is None:
            return
        stream = self.__streams.pop(env.correlation_id, None)
        if stream is None:
            return
        self.__transcript.record(
            "note",
            "stream_assembled",
            {
                "stream": stream["stream"],
                "text": "".join(cast(list[str], stream["parts"])),
            },
            correlation_id=env.correlation_id,
        )

    # ------------------------------------------------------------------
    # Server→client requests (the simulated user)
    # ------------------------------------------------------------------

    def __on_server_request(self, env: Envelope) -> None:
        self.__pending_server_requests += 1
        task = asyncio.create_task(self.__answer_server_request(env), name="validator-answer")
        self.__responder_tasks.add(task)
        task.add_done_callback(self.__responder_tasks.discard)

    async def __answer_server_request(self, env: Envelope) -> None:
        request_type = str(env.payload.get("type", ""))
        try:
            response = await self.__build_answer(request_type, env.payload)
        except Exception:
            _log.exception("User simulator failed on %s", request_type)
            response = {"error": "simulator_failure"}
        try:
            ws = self.__ws
            if ws is not None and not ws.closed:
                reply = Envelope.make_response(env.id, response)
                self.__transcript.record("send", "response", response, correlation_id=env.id)
                await ws.send_str(reply.to_json())
            self.__transcript.record_interaction(request_type, env.payload, response)
        finally:
            self.__pending_server_requests -= 1
            async with self.__changed:
                self.__changed.notify_all()

    async def __build_answer(
        self, request_type: str, payload: dict[str, object]
    ) -> dict[str, object]:
        if request_type == SREQ_PROMPT_QUESTION:
            return await self.__user.answer_questions(payload)
        if request_type == SREQ_PROMPT_APPROVAL:
            return await self.__user.answer_approval(payload)
        if request_type == SREQ_PROMPT_PERMISSION:
            return await self.__user.answer_permission(payload)
        if request_type == SREQ_API_KEY_REQUEST:
            key = await self.__user.provide_api_key(str(payload.get("vendor", "")))
            return {"api_key": key} if key else {"error": "cancelled"}
        _log.warning("Unsupported server request type: %s", request_type)
        return {"error": "unsupported_request"}

    def __fail_pending(self, error: Exception) -> None:
        for future in self.__pending.values():
            if not future.done():
                future.set_exception(error)
        self.__pending.clear()

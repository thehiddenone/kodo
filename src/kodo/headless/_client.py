"""The headless run's WebSocket client: one session, one prompt, no user.

Plays the VS Code extension's role over the documented wire contract
(doc/WS_PROTOCOL.md): ``hello``, session setup requests, the prompt, and —
since nobody is present — a deterministic answer to every server→client
request. Every frame worth showing becomes a stdout event
(:class:`~._events.EventSink`), and a running tally feeds the final
:class:`~._result.RunResult`.

Turn-end detection is the validator's: the phase was seen ``running`` and
now rests (``awaiting_user`` / ``done`` / ``stopped`` / ``error``) with no
answer in flight, stable across a short settle window — a resting phase is
also what a pending question looks like, and the engine flips back to
``running`` right after an answer lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path
from typing import cast

import aiohttp

from kodo.common import Envelope
from kodo.transport import (
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_AGENT_TOOL_CALL_DETAIL,
    EVT_AGENT_TOOL_CALL_PREP,
    EVT_AUTONOMOUS_CHANGED,
    EVT_ERROR,
    EVT_LLM_TURN_START,
    EVT_NUDGE,
    EVT_STATE,
    EVT_SUBSESSION_ENDED,
    EVT_SUBSESSION_STARTED,
    EVT_USAGE_UPDATE,
    MSG_HELLO,
    MSG_STOP,
    SREQ_API_KEY_REQUEST,
    SREQ_HF_TOKEN_REQUEST,
    SREQ_PROMPT_APPROVAL,
    SREQ_PROMPT_CHOOSE_PROJECT_FOLDER,
    SREQ_PROMPT_EDIT_REVIEW,
    SREQ_PROMPT_PERMISSION,
    SREQ_PROMPT_QUESTION,
    SREQ_PROMPT_STUCK_ALERT,
    SREQ_WORKSPACE_CONFIRM_FOLDER,
)

from ._events import EventSink

__all__ = ["HEADLESS_ANSWERED_REQUESTS", "HeadlessClient", "RequestError"]

_log = logging.getLogger(__name__)

_RESTING_PHASES = frozenset({"intake", "awaiting_user", "done", "stopped", "error"})
_REQUEST_TIMEOUT = 60.0
_DENIAL_MARKER = "Blocked by the headless sandbox"
# Events that close any open thinking/text segment first, so the stdout order
# matches what the agent did (its words, then the call they led to).
_SEGMENT_BOUNDARIES = frozenset(
    {
        EVT_LLM_TURN_START,
        EVT_AGENT_TOOL_CALL_PREP,
        EVT_SUBSESSION_STARTED,
        EVT_SUBSESSION_ENDED,
        EVT_AGENT_FINISHED,
        EVT_ERROR,
    }
)
_NO_USER_ANSWER = (
    "No user is available in this headless run. Proceed with your best judgement "
    "and do not ask again."
)

#: Every server→client request type this client answers deterministically.
HEADLESS_ANSWERED_REQUESTS = frozenset(
    {
        SREQ_PROMPT_QUESTION,
        SREQ_PROMPT_APPROVAL,
        SREQ_PROMPT_PERMISSION,
        SREQ_PROMPT_STUCK_ALERT,
        SREQ_PROMPT_EDIT_REVIEW,
        SREQ_PROMPT_CHOOSE_PROJECT_FOLDER,
        SREQ_WORKSPACE_CONFIRM_FOLDER,
        SREQ_API_KEY_REQUEST,
        SREQ_HF_TOKEN_REQUEST,
    }
)


class RequestError(RuntimeError):
    """The server answered a request with an error payload."""


class _Usage:
    """Per-model / per-agent call accounting."""

    __rows: dict[str, dict[str, float]]

    def __init__(self) -> None:
        self.__rows = {}

    @property
    def rows(self) -> dict[str, dict[str, float]]:
        return {key: dict(value) for key, value in self.__rows.items()}

    def add(self, key: str, input_tokens: int, output_tokens: int, usd: float) -> None:
        row = self.__rows.setdefault(
            key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0}
        )
        row["calls"] += 1
        row["input_tokens"] += input_tokens
        row["output_tokens"] += output_tokens
        row["usd"] = round(row["usd"] + usd, 6)


class HeadlessClient:
    """One WebSocket connection driving one autonomous kodo session."""

    __url: str
    __sink: EventSink
    __root: Path
    __stream_deltas: bool
    __window_id: str
    __http: aiohttp.ClientSession | None
    __ws: aiohttp.ClientWebSocketResponse | None
    __recv_task: asyncio.Task[None] | None
    __pending: dict[str, asyncio.Future[dict[str, object]]]
    __responders: set[asyncio.Task[None]]
    __changed: asyncio.Condition
    __closed: bool
    __session_id: str
    __phase: str
    __saw_running: bool
    __current_agent: str
    __subsessions: list[tuple[str, str]]
    __streams: dict[str, tuple[str, str, str | None, list[str]]]
    __tool_names: dict[str, str]
    __assistant_text: str
    __cumulative: dict[str, float]
    __by_model: _Usage
    __by_agent: _Usage
    __tool_calls: int
    __tool_denials: int
    __questions: int
    __nudges: int
    __event_seq: int
    __last_error: tuple[int, str] | None
    __last_text_seq: int
    __autonomous_dropped: bool

    def __init__(
        self, url: str, sink: EventSink, sandbox_root: Path, *, stream_deltas: bool = False
    ) -> None:
        """Bind the connection target and the output.

        Args:
            url (str): The server's ``ws://…/ws`` endpoint.
            sink (EventSink): Where events are written.
            sandbox_root (Path): The single workspace root (answers folder
                requests; never changed by this client).
            stream_deltas (bool): Emit every thinking/text chunk as it
                arrives instead of one event per completed stream.
        """
        self.__url = url
        self.__sink = sink
        self.__root = sandbox_root
        self.__stream_deltas = stream_deltas
        self.__window_id = f"kodo-headless-{uuid.uuid4().hex[:8]}"
        self.__http = None
        self.__ws = None
        self.__recv_task = None
        self.__pending = {}
        self.__responders = set()
        self.__changed = asyncio.Condition()
        self.__closed = False
        self.__session_id = ""
        self.__phase = ""
        self.__saw_running = False
        self.__current_agent = ""
        self.__subsessions = []
        self.__streams = {}
        self.__tool_names = {}
        self.__assistant_text = ""
        self.__cumulative = {}
        self.__by_model = _Usage()
        self.__by_agent = _Usage()
        self.__tool_calls = 0
        self.__tool_denials = 0
        self.__questions = 0
        self.__nudges = 0
        self.__event_seq = 0
        self.__last_error = None
        self.__last_text_seq = 0
        self.__autonomous_dropped = False

    # ------------------------------------------------------------------
    # Tally (read by the run after the turn)
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """The bound session id (``""`` before :meth:`hello`)."""
        return self.__session_id

    @property
    def phase(self) -> str:
        """The latest session phase."""
        return self.__phase

    @property
    def assistant_text(self) -> str:
        """The top-level agent's most recent visible response."""
        return self.__assistant_text

    @property
    def cumulative(self) -> dict[str, float]:
        """The latest ``usage.update`` running totals."""
        return dict(self.__cumulative)

    @property
    def per_model(self) -> dict[str, dict[str, float]]:
        """Per-model call accounting."""
        return self.__by_model.rows

    @property
    def per_agent(self) -> dict[str, dict[str, float]]:
        """Per-agent call accounting."""
        return self.__by_agent.rows

    @property
    def tool_calls(self) -> int:
        """Tool calls dispatched."""
        return self.__tool_calls

    @property
    def tool_denials(self) -> int:
        """Calls the headless sandbox refused."""
        return self.__tool_denials

    @property
    def questions_asked(self) -> int:
        """Questions asked of the absent user."""
        return self.__questions

    @property
    def nudges(self) -> int:
        """Stuck-watchdog nudges."""
        return self.__nudges

    @property
    def turn_error(self) -> str | None:
        """The error that ended the turn, if one arrived after its last response."""
        if self.__last_error is None:
            return None
        seq, message = self.__last_error
        return message if seq > self.__last_text_seq else None

    @property
    def autonomous_dropped(self) -> bool:
        """Whether the engine left autonomous mode during the run."""
        return self.__autonomous_dropped

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket and start receiving."""
        self.__http = aiohttp.ClientSession()
        self.__ws = await self.__http.ws_connect(self.__url, max_msg_size=0)
        self.__recv_task = asyncio.create_task(self.__recv_loop(), name="headless-recv")

    async def hello(self) -> dict[str, object]:
        """Handshake and bind a brand-new session.

        Returns:
            dict[str, object]: The ``hello.ack`` payload.
        """
        ack = await self.request(
            MSG_HELLO,
            {"client": "kodo-headless", "version": "1", "window_id": self.__window_id},
            session_scoped=False,
        )
        self.__session_id = str(ack.get("session_id", ""))
        state = ack.get("state")
        if isinstance(state, dict):
            self.__apply_phase(cast(dict[str, object], state))
        return ack

    async def close(self) -> None:
        """Close the connection (idempotent)."""
        self.__closed = True
        for task in list(self.__responders):
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

    async def request(
        self,
        msg_type: str,
        payload: dict[str, object] | None = None,
        *,
        session_scoped: bool = True,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> dict[str, object]:
        """Send one request and await its response payload.

        Args:
            msg_type (str): The ``payload.type``.
            payload (dict[str, object] | None): Message fields.
            session_scoped (bool): Attach the bound ``session_id``.
            timeout (float): Seconds to wait for the response.

        Returns:
            dict[str, object]: The response payload.

        Raises:
            RequestError: The response carries an error.
            ConnectionError: The socket is not open.
            TimeoutError: No response within *timeout*.
        """
        ws = self.__ws
        if ws is None or ws.closed or self.__closed:
            raise ConnectionError("WebSocket is not connected")
        body: dict[str, object] = {"type": msg_type, **(payload or {})}
        if session_scoped:
            body["session_id"] = self.__session_id
        env = Envelope(kind="request", payload=body)
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.__pending[env.id] = future
        await ws.send_str(env.to_json())
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.__pending.pop(env.id, None)
        if response.get("error") or response.get("type") == "error":
            raise RequestError(f"{msg_type} failed: {response}")
        return response

    def begin_turn(self) -> None:
        """Reset turn tracking; call right before submitting the prompt."""
        self.__saw_running = False

    async def wait_turn_end(self, *, timeout: float, settle_seconds: float = 2.0) -> str:
        """Block until the turn has finished.

        Args:
            timeout (float): Overall seconds before giving up.
            settle_seconds (float): How long the resting condition must hold.

        Returns:
            str: The resting phase.

        Raises:
            TimeoutError: The turn did not finish within *timeout*.
            ConnectionError: The connection dropped.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Turn did not finish within {timeout:.0f}s")
            async with self.__changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self.__changed.wait_for(self.__turn_ended), timeout=remaining
                    )
            if self.__closed:
                raise ConnectionError("Connection to the kodo server closed mid-turn")
            if not self.__turn_ended():
                continue
            await asyncio.sleep(settle_seconds)
            if self.__turn_ended():
                return self.__phase

    async def stop(self) -> None:
        """Ask the server to stop the in-flight turn (best effort)."""
        with contextlib.suppress(Exception):
            await self.request(MSG_STOP, timeout=10.0)

    def __turn_ended(self) -> bool:
        if self.__closed:
            return True
        return self.__saw_running and self.__phase in _RESTING_PHASES and not self.__responders

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
                    _log.warning("Undecodable frame: %r", msg.data)
                    continue
                await self.__dispatch(env)
        except Exception:
            _log.exception("Receive pump failed")
        finally:
            self.__closed = True
            self.__fail_pending(ConnectionError("Connection closed"))
            async with self.__changed:
                self.__changed.notify_all()

    async def __dispatch(self, env: Envelope) -> None:
        if env.kind == "response":
            future = self.__pending.get(env.correlation_id or "")
            if future is not None and not future.done():
                future.set_result(env.payload)
            return
        if env.kind == "event":
            self.__on_event(env.payload)
            async with self.__changed:
                self.__changed.notify_all()
            return
        if env.kind in ("stream_chunk", "thinking_chunk"):
            self.__on_chunk(env)
            return
        if env.kind == "stream_end":
            self.__on_stream_end(env)
            return
        if env.kind == "request":
            task = asyncio.create_task(self.__answer(env), name="headless-answer")
            self.__responders.add(task)
            task.add_done_callback(self.__responders.discard)

    # ------------------------------------------------------------------
    # Events → stdout + tally
    # ------------------------------------------------------------------

    def __scope(self) -> dict[str, object]:
        subsession = self.__subsessions[-1][0] if self.__subsessions else None
        return {"agent": self.__current_agent or None, "subsession_id": subsession}

    def __on_event(self, payload: dict[str, object]) -> None:
        kind = str(payload.get("type", ""))
        if kind in _SEGMENT_BOUNDARIES:
            self.__flush_streams()
        self.__event_seq += 1
        if kind == EVT_STATE:
            self.__apply_phase(payload)
        elif kind == EVT_LLM_TURN_START:
            self.__current_agent = str(payload.get("agent", "") or "")
            self.__sink.emit("llm.turn", **self.__scope(), model=payload.get("model"))
        elif kind in (EVT_AGENT_STARTED, EVT_AGENT_FINISHED):
            name = "agent.start" if kind == EVT_AGENT_STARTED else "agent.finish"
            self.__sink.emit(
                name,
                agent=payload.get("agent"),
                subsession_id=self.__scope()["subsession_id"],
                status=payload.get("status"),
            )
        elif kind == EVT_SUBSESSION_STARTED:
            subsession_id = str(payload.get("subsession_id", ""))
            agent = str(payload.get("agent", ""))
            self.__subsessions.append((subsession_id, agent))
            self.__sink.emit(
                "subsession.start",
                agent=agent,
                subsession_id=subsession_id,
                task=payload.get("task"),
            )
        elif kind == EVT_SUBSESSION_ENDED:
            subsession_id = str(payload.get("subsession_id", ""))
            self.__subsessions = [s for s in self.__subsessions if s[0] != subsession_id]
            self.__sink.emit(
                "subsession.end",
                agent=payload.get("agent"),
                subsession_id=subsession_id,
                status=payload.get("status"),
            )
        elif kind == EVT_AGENT_TOOL_CALL_PREP:
            tool_call_id = str(payload.get("tool_call_id", ""))
            tool = str(payload.get("tool_name", ""))
            self.__tool_names[tool_call_id] = tool
            self.__tool_calls += 1
            self.__sink.emit("tool.start", **self.__scope(), tool=tool, tool_call_id=tool_call_id)
        elif kind == EVT_AGENT_TOOL_CALL_DETAIL:
            self.__on_tool_detail(payload)
        elif kind == EVT_USAGE_UPDATE:
            self.__on_usage(payload)
        elif kind == EVT_NUDGE:
            self.__nudges += 1
            self.__sink.emit(
                "nudge", **self.__scope(), reasons=payload.get("reasons"), mode=payload.get("mode")
            )
        elif kind == EVT_AUTONOMOUS_CHANGED:
            self.__autonomous_dropped = True
            self.__sink.emit("autonomous.changed", **self.__scope(), detail=payload)
        elif kind == EVT_ERROR:
            message = str(payload.get("message", ""))
            self.__last_error = (self.__event_seq, message)
            self.__sink.emit(
                "error",
                **self.__scope(),
                code=payload.get("code"),
                message=message,
                recoverable=payload.get("recoverable"),
            )

    def __apply_phase(self, state: dict[str, object]) -> None:
        phase = state.get("phase")
        if isinstance(phase, str) and phase != self.__phase:
            self.__phase = phase
            if phase == "running":
                self.__saw_running = True
            self.__sink.emit("phase", phase=phase)

    def __on_tool_detail(self, payload: dict[str, object]) -> None:
        tool_call_id = str(payload.get("tool_call_id", ""))
        tool = self.__tool_names.get(tool_call_id, "")
        document = ""
        file_path = payload.get("file")
        if isinstance(file_path, str) and file_path:
            with contextlib.suppress(OSError):
                document = Path(file_path).read_text(encoding="utf-8")
        self.__sink.emit(
            "tool.call",
            **self.__scope(),
            tool=tool,
            tool_call_id=tool_call_id,
            document=document,
            rows=payload.get("rows"),
            schema_compliance=payload.get("schema_compliance"),
        )
        if _DENIAL_MARKER in document:
            self.__tool_denials += 1
            reason = next(
                (line.strip() for line in document.splitlines() if _DENIAL_MARKER in line),
                _DENIAL_MARKER,
            )
            self.__sink.emit(
                "tool.denied", **self.__scope(), tool=tool, tool_call_id=tool_call_id, reason=reason
            )

    def __on_usage(self, payload: dict[str, object]) -> None:
        for key in (
            "cumulative_usd",
            "cumulative_input_tokens",
            "cumulative_input_tokens_uncached",
            "cumulative_output_tokens",
        ):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                self.__cumulative[key] = float(value)
        last = payload.get("last_call_tokens")
        if not isinstance(last, dict):
            return  # the post-hello snapshot: totals only, no call
        tokens = cast(dict[str, object], last)

        def _int(name: str) -> int:
            value = tokens.get(name)
            return int(value) if isinstance(value, (int, float)) else 0

        input_tokens = _int("input") + _int("cache_read") + _int("cache_write")
        output_tokens = _int("output")
        usd_raw = payload.get("usd_cost")
        usd = float(usd_raw) if isinstance(usd_raw, (int, float)) else 0.0
        model = str(payload.get("model", "") or "unknown")
        agent = str(payload.get("agent", "") or self.__current_agent or "unknown")
        self.__by_model.add(model, input_tokens, output_tokens, usd)
        self.__by_agent.add(agent, input_tokens, output_tokens, usd)
        self.__sink.emit(
            "usage",
            **self.__scope(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
            stop_reason=payload.get("stop_reason"),
            duration_seconds=payload.get("duration_seconds"),
        )

    def __on_chunk(self, env: Envelope) -> None:
        # The server keeps ONE stream id for a whole agent turn — every LLM
        # round, thinking and text chunks interleaved, one `stream_end` at the
        # very end — so a stream is cut into segments: a new one starts
        # whenever the chunk kind flips (and at round/tool boundaries, see
        # __flush_streams), and each segment is one `thinking`/`text` event.
        stream_id = env.correlation_id or ""
        kind = "thinking" if env.kind == "thinking_chunk" else "text"
        text = str(env.payload.get("text", ""))
        scope = self.__scope()
        open_segment = self.__streams.get(stream_id)
        if open_segment is not None and open_segment[0] != kind:
            self.__flush_stream(stream_id)
            open_segment = None
        if open_segment is None:
            self.__streams[stream_id] = (
                kind,
                str(scope["agent"] or ""),
                cast(str | None, scope["subsession_id"]),
                [],
            )
        self.__streams[stream_id][3].append(text)
        if self.__stream_deltas and text:
            self.__sink.emit(f"{kind}.delta", **scope, text=text)

    def __on_stream_end(self, env: Envelope) -> None:
        self.__flush_stream(env.correlation_id or "")

    def __flush_streams(self) -> None:
        """Close every open segment — before an event that starts a new one."""
        for stream_id in list(self.__streams):
            self.__flush_stream(stream_id)

    def __flush_stream(self, stream_id: str) -> None:
        segment = self.__streams.pop(stream_id, None)
        if segment is None:
            return
        kind, agent, subsession_id, parts = segment
        text = "".join(parts)
        if not text.strip():
            return
        if kind == "text" and subsession_id is None:
            self.__assistant_text = text
            self.__event_seq += 1
            self.__last_text_seq = self.__event_seq
        if not self.__stream_deltas:
            self.__sink.emit(kind, agent=agent or None, subsession_id=subsession_id, text=text)

    # ------------------------------------------------------------------
    # Server → client requests: deterministic, never blocking
    # ------------------------------------------------------------------

    async def __answer(self, env: Envelope) -> None:
        request_type = str(env.payload.get("type", ""))
        response = self.__build_answer(request_type, env.payload)
        ws = self.__ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.send_str(Envelope.make_response(env.id, response).to_json())
        async with self.__changed:
            self.__changed.notify_all()

    def __build_answer(self, request_type: str, payload: dict[str, object]) -> dict[str, object]:
        if request_type == SREQ_PROMPT_QUESTION:
            questions = cast(list[dict[str, object]], payload.get("questions") or [])
            self.__questions += len(questions)
            answers: list[dict[str, object]] = []
            for question in questions:
                options = cast(list[object], question.get("options") or [])
                selected = [str(options[0])] if options else []
                answers.append({"selected": selected, "free_text": _NO_USER_ANSWER})
            self.__sink.emit("question", **self.__scope(), questions=questions, answers=answers)
            return {"type": "prompt.question.response", "answers": answers}
        if request_type == SREQ_PROMPT_APPROVAL:
            return {"type": "prompt.approval.response", "action": "agree", "feedback_text": None}
        if request_type == SREQ_PROMPT_PERMISSION:
            return {
                "type": "prompt.permission.response",
                "action": "deny",
                "feedback": "No user is present to grant permission in this headless run.",
            }
        if request_type == SREQ_PROMPT_STUCK_ALERT:
            return {"type": "prompt.stuck_alert.response", "action": "unstick"}
        if request_type == SREQ_PROMPT_EDIT_REVIEW:
            return {"type": "prompt.edit_review.response", "action": "approve", "feedback": []}
        if request_type == SREQ_PROMPT_CHOOSE_PROJECT_FOLDER:
            return {"error": "cancelled"}
        if request_type == SREQ_WORKSPACE_CONFIRM_FOLDER:
            return {
                "attached": False,
                "error": f"The workspace of this headless run is fixed to {self.__root}.",
            }
        if request_type == SREQ_API_KEY_REQUEST:
            vendor = str(payload.get("vendor", "")).upper().replace("-", "_")
            key = os.environ.get(f"{vendor}_API_KEY", "")
            return {"api_key": key} if key else {"error": "cancelled"}
        if request_type == SREQ_HF_TOKEN_REQUEST:
            return {"hf_token": os.environ.get("HF_TOKEN", "")}
        self.__sink.emit("warning", message=f"unanswerable server request {request_type!r}")
        return {"error": "unsupported_request"}

    def __fail_pending(self, error: Exception) -> None:
        for future in self.__pending.values():
            if not future.done():
                future.set_exception(error)
        self.__pending.clear()

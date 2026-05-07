"""Workflow engine: single async worker, stage machine, prompt dispatch.

For M2 the engine does not run full agents.  It accepts ``prompt.submit``
messages, calls the LLM plugin with the raw text, streams tokens back to the
WebView, and records cost in transient state.  The full agent pipeline lands
in M3.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kodo.llms._interface import (
    Message,
    StreamEvent,
    TokenDelta,
    TurnEnd,
)
from kodo.llms.anthropic import UnrecoverableError
from kodo.transport._envelope import Envelope
from kodo.transport._messages import EVT_ERROR, EVT_STATE, EVT_USAGE_UPDATE

from ._session import SessionState
from ._stages import Stage

if TYPE_CHECKING:
    from kodo.llms._interface import LLMPlugin
    from kodo.state._transient import TransientStore
    from kodo.transport._ws import AppState

__all__ = ["WorkflowEngine"]

_log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_SYSTEM_PROMPT = (
    "You are Kōdo, an agentic software-building assistant. "
    "Answer concisely and helpfully."
)


class WorkflowEngine:
    """Single-worker workflow engine that processes prompts via the LLM plugin.

    For M2 the engine handles raw ``prompt.submit`` messages from the
    WebView, calls the Anthropic LLM plugin directly, and streams the
    response back.  Approval gates and the full agent pipeline are M3+.
    """

    __app_state: AppState
    __llm: LLMPlugin
    __transient: TransientStore
    __queue: asyncio.Queue[dict[str, object]]
    __session: SessionState
    __worker: asyncio.Task[None] | None
    __cumulative_usd: float

    def __init__(
        self,
        app_state: AppState,
        llm: LLMPlugin,
        transient: TransientStore,
    ) -> None:
        """Initialise the engine.

        Args:
            app_state (AppState): WebSocket application state for sending events.
            llm (LLMPlugin): LLM provider plugin.
            transient (TransientStore): Transient JSONL store for this session.
        """
        self.__app_state = app_state
        self.__llm = llm
        self.__transient = transient
        self.__queue = asyncio.Queue()
        self.__session = SessionState()
        self.__worker = None
        self.__cumulative_usd = 0.0

    @property
    def session(self) -> SessionState:
        """Current session state snapshot."""
        return self.__session

    async def start(self) -> None:
        """Start the single worker coroutine."""
        self.__worker = asyncio.create_task(self.__run_worker(), name="kodo-worker")
        _log.info("Workflow worker started (session=%s)", self.__session.session_id)

    async def stop(self) -> None:
        """Cancel the worker and wait for it to finish cleanly."""
        if self.__worker is not None:
            self.__worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.__worker
            self.__worker = None
        self.__session.stage = Stage.STOPPED
        await self.__emit_state()
        _log.info("Workflow worker stopped")

    async def handle_prompt_submit(self, text: str, request_id: str) -> None:
        """Enqueue a user-submitted prompt for LLM processing.

        Args:
            text (str): The prompt text from the WebView.
            request_id (str): Correlation ID from the originating request.
        """
        await self.__queue.put({"text": text, "request_id": request_id})

    async def handle_mode_set(self, autonomous: bool) -> None:
        """Toggle autonomous mode.

        Args:
            autonomous (bool): New autonomous mode flag.
        """
        self.__session.autonomous = autonomous
        self.__transient.meta.update(autonomous=autonomous)
        await self.__emit_state()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def __run_worker(self) -> None:
        while True:
            task = await self.__queue.get()
            text = str(task.get("text", ""))
            request_id = str(task.get("request_id", uuid.uuid4().hex))
            try:
                await self.__process_prompt(text, request_id)
            except asyncio.CancelledError:
                raise
            except UnrecoverableError as exc:
                _log.error("Unrecoverable LLM error (HTTP %d): %s", exc.status_code, exc)
                await self.__emit_error(str(exc), recoverable=False)
                self.__session.stage = Stage.ERROR
                self.__session.agent = None
                await self.__emit_state()
            except Exception as exc:
                _log.exception("Unhandled error in workflow worker: %s", exc)
                await self.__emit_error(str(exc), recoverable=True)
                self.__session.stage = Stage.IDLE
                self.__session.agent = None
                await self.__emit_state()
            finally:
                self.__queue.task_done()

    async def __process_prompt(self, text: str, request_id: str) -> None:
        stream_id = uuid.uuid4().hex

        self.__session.stage = Stage.NARRATIVE
        self.__session.agent = "raw"
        await self.__emit_state()

        messages = [Message(role="user", content=text)]
        call_start = datetime.now(tz=UTC).isoformat()

        turn_end: TurnEnd | None = None

        try:
            async for event in self.__llm.stream_query(
                stream_id=stream_id,
                model=_DEFAULT_MODEL,
                system=_SYSTEM_PROMPT,
                messages=messages,
                tools=[],
                cache_breakpoints=[0],
            ):
                await self.__handle_stream_event(event, stream_id)
                if isinstance(event, TurnEnd):
                    turn_end = event
        finally:
            await self.__app_state.send(Envelope.make_stream_end(stream_id))

        if turn_end is not None:
            self.__cumulative_usd += turn_end.usage.usd_cost
            await self.__emit_usage(turn_end)
            await self.__transient.write_agent_record(
                "raw",
                {
                    "call_start": call_start,
                    "call_end": datetime.now(tz=UTC).isoformat(),
                    "model": _DEFAULT_MODEL,
                    "prompt_preview": text[:200],
                    "input_tokens": turn_end.usage.input_tokens,
                    "output_tokens": turn_end.usage.output_tokens,
                    "cache_write_tokens": turn_end.usage.cache_write_tokens,
                    "cache_read_tokens": turn_end.usage.cache_read_tokens,
                    "usd_cost": turn_end.usage.usd_cost,
                    "cumulative_usd": self.__cumulative_usd,
                    "stop_reason": turn_end.stop_reason,
                },
            )

        self.__session.stage = Stage.IDLE
        self.__session.agent = None
        await self.__emit_state()

    async def __handle_stream_event(self, event: StreamEvent, stream_id: str) -> None:
        if isinstance(event, TokenDelta):
            await self.__app_state.send(Envelope.make_stream_chunk(stream_id, event.text))

    async def __emit_state(self) -> None:
        await self.__app_state.send(
            Envelope.make_event(EVT_STATE, self.__session.to_dict())
        )
        self.__transient.meta.update(stage=self.__session.stage.value)

    async def __emit_usage(self, turn_end: TurnEnd) -> None:
        await self.__app_state.send(
            Envelope.make_event(
                EVT_USAGE_UPDATE,
                {
                    "cumulative_usd": round(self.__cumulative_usd, 6),
                    "last_call_tokens": {
                        "input": turn_end.usage.input_tokens,
                        "output": turn_end.usage.output_tokens,
                        "cache_write": turn_end.usage.cache_write_tokens,
                        "cache_read": turn_end.usage.cache_read_tokens,
                    },
                    "breakdown": {
                        "raw": round(self.__cumulative_usd, 6),
                    },
                },
            )
        )

    async def __emit_error(self, message: str, *, recoverable: bool) -> None:
        await self.__app_state.send(
            Envelope.make_event(
                EVT_ERROR,
                {
                    "code": "workflow_error",
                    "message": message,
                    "recoverable": recoverable,
                },
            )
        )

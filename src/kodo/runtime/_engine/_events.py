"""Client event emitters — every engine-originated envelope in one place.

:class:`EngineEmitters` is a plain collaborator: it owns the outbound
``MessageSink`` traffic *and* the running cumulative USD cost (every LLM
call's cost is folded in through :meth:`add_cost`, visible and silent alike).
The context-gauge payload is engine state owned by the
:class:`~._compaction.ContextCompactor`, so it is injected as a callable
rather than duplicated here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from kodo.common import Envelope, MessageSink
from kodo.llms import (
    StreamEvent,
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    TurnEnd,
)
from kodo.transport import (
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_AGENT_TOOL_CALL_IN_PROGRESS,
    EVT_CONTEXT_COMPACTING,
    EVT_CONTEXT_STATS,
    EVT_ERROR,
    EVT_SECURITY_JUDGING,
    EVT_SESSION_NAMING,
    EVT_STATE,
    EVT_USAGE_UPDATE,
)

from .._session import SessionState

_log = logging.getLogger(__name__)


class EngineEmitters:
    """Sends the engine's client-facing events and tracks cumulative cost.

    Args:
        sink: Sends outbound envelopes to the client.
        session: Live session state (read for ``session.state`` payloads and
            the agent-started/finished ``component`` field).
        context_stats: Returns the current ``context.stats`` payload — owned
            by the compactor, late-bound via this callable so the two
            collaborators need no mutual reference.
    """

    def __init__(
        self,
        sink: MessageSink,
        session: SessionState,
        context_stats: Callable[[], dict[str, object]],
    ) -> None:
        self._sink = sink
        self._session = session
        self._context_stats = context_stats
        self._cumulative_usd = 0.0

    @property
    def cumulative_usd(self) -> float:
        """Running total of every LLM call's USD cost this session."""
        return self._cumulative_usd

    def add_cost(self, usd: float) -> None:
        """Fold one LLM call's cost into the running session total."""
        self._cumulative_usd += usd

    async def handle_stream_event(self, event: StreamEvent, stream_id: str) -> None:
        """Forward a streaming LLM event to the client feed."""
        if isinstance(event, ThinkingDelta):
            await self._sink.send(Envelope.make_thinking_chunk(stream_id, event.text))
        elif isinstance(event, TokenDelta):
            await self._sink.send(Envelope.make_stream_chunk(stream_id, event.text))
        elif isinstance(event, ToolCallArgDelta):
            await self._sink.send(
                Envelope.make_toolgen_chunk(stream_id, event.tool_name, event.text)
            )

    async def emit_state(self) -> None:
        """Push the session state snapshot (plus the dependent context gauge)."""
        await self._sink.send(Envelope.make_event(EVT_STATE, self._session.to_dict()))
        # The header context gauge and its "Compact now" enablement both depend
        # on phase, so refresh them whenever state is pushed.
        await self.emit_context_stats()

    async def emit_context_stats(self) -> None:
        """Push the live context gauge (current/limit/percent + compactability)."""
        await self._sink.send(Envelope.make_event(EVT_CONTEXT_STATS, self._context_stats()))

    async def emit_context_compacting(self, active: bool) -> None:
        """Bracket a compaction run so the client shows a "Compacting…" banner."""
        await self._sink.send(Envelope.make_event(EVT_CONTEXT_COMPACTING, {"active": active}))

    async def emit_usage(self, turn_end: TurnEnd, model: str, duration_seconds: float) -> None:
        """Push a per-call usage record (tokens, model, running cost)."""
        await self._sink.send(
            Envelope.make_event(
                EVT_USAGE_UPDATE,
                {
                    "cumulative_usd": round(self._cumulative_usd, 6),
                    "duration_seconds": round(duration_seconds, 3),
                    "last_call_tokens": {
                        "input": turn_end.usage.input_tokens,
                        "output": turn_end.usage.output_tokens,
                        "cache_write": turn_end.usage.cache_write_tokens,
                        "cache_read": turn_end.usage.cache_read_tokens,
                    },
                    "model": model,
                    "breakdown": {},
                },
            )
        )

    async def emit_session_naming(self, active: bool) -> None:
        """Tell the client whether the silent session-titler call is running.

        Drives a transient "Naming session …" indicator in the WebView so the
        titling round-trip (which streams nothing) does not look like a stall.
        """
        await self._sink.send(Envelope.make_event(EVT_SESSION_NAMING, {"active": active}))

    async def emit_security_judging(self, active: bool) -> None:
        """Tell the client whether the security layer's silent intent-judge
        LLM call is running — drives a transient "Evaluating…" indicator so
        the (potentially long) judge round-trip does not look like a stall.
        """
        await self._sink.send(Envelope.make_event(EVT_SECURITY_JUDGING, {"active": active}))

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        """Tell the client a tool call has cleared the security gate and is now
        actually running (doc/SECURITY.md §6). Sent from
        ``ToolDispatcher.dispatch`` right after ``__security_gate`` returns —
        allowed outright, or the user granted permission — and right before
        the tool handler runs, so the client's run_command timeout animation
        starts on real execution time instead of ticking through any judging
        round or permission wait that preceded it.
        """
        await self._sink.send(
            Envelope.make_event(EVT_AGENT_TOOL_CALL_IN_PROGRESS, {"tool_call_id": tool_call_id})
        )

    async def emit_cost_only(self) -> None:
        """Push a cost-only ``usage.update`` (no per-call token entry).

        With ``last_call_tokens`` set to ``None`` the client updates the running
        session-cost figure without appending a status entry to the feed — used
        to fold an invisible call's cost (e.g. session titling) into the total.
        """
        await self._sink.send(
            Envelope.make_event(
                EVT_USAGE_UPDATE,
                {
                    "cumulative_usd": round(self._cumulative_usd, 6),
                    "duration_seconds": 0.0,
                    "last_call_tokens": None,
                    "model": "",
                    "breakdown": {},
                },
            )
        )

    async def emit_error(self, message: str, *, recoverable: bool) -> None:
        """Push a user-facing runtime error."""
        await self._sink.send(
            Envelope.make_event(
                EVT_ERROR,
                {
                    "code": "runtime_error",
                    "message": message,
                    "recoverable": recoverable,
                },
            )
        )

    async def emit_agent_started(self, agent_name: str) -> None:
        """Announce that *agent_name* took the floor."""
        await self._sink.send(
            Envelope.make_event(
                EVT_AGENT_STARTED,
                {"agent": agent_name, "component": self._session.component},
            )
        )

    async def emit_agent_finished(self, agent_name: str) -> None:
        """Announce that *agent_name* handed the floor back."""
        await self._sink.send(
            Envelope.make_event(
                EVT_AGENT_FINISHED,
                {
                    "agent": agent_name,
                    "component": self._session.component,
                    "status": "ok",
                },
            )
        )

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
from kodo.state import TransientStore
from kodo.transport import (
    EVT_AGENT_CYCLIC_THINKING_CRITICAL,
    EVT_AGENT_CYCLIC_THINKING_NOTICE,
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_AGENT_STUCK_CRITICAL,
    EVT_AGENT_TOOL_CALL_IN_PROGRESS,
    EVT_AGENT_UNSTUCK_NUDGE,
    EVT_CONTEXT_COMPACTING,
    EVT_CONTEXT_STATS,
    EVT_ERROR,
    EVT_SECURITY_RULE_ADDED,
    EVT_SESSION_NAMING,
    EVT_STATE,
    EVT_USAGE_UPDATE,
    EVT_WEB_SEARCH_NOTE,
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
        transient: Append-only JSONL session store, used by every marker-
            persisting emitter (:meth:`_append_marker`) to durably record the
            event alongside the live push.
    """

    def __init__(
        self,
        sink: MessageSink,
        session: SessionState,
        context_stats: Callable[[], dict[str, object]],
        transient: TransientStore,
    ) -> None:
        self._sink = sink
        self._session = session
        self._context_stats = context_stats
        self._transient = transient
        self._cumulative_usd = 0.0

    def _append_marker(self, marker: dict[str, object]) -> None:
        """Persist *marker* to whichever container is currently active.

        The active subsession's own log if one is running, else the main
        session log — every marker-persisting emitter goes through this so a
        subsession's own events (its usage stats, an error mid-run, its
        agent stalling) land in its own log, never bleeding into the
        parent's. Mirrors :attr:`~kodo.state.TransientStore.active_subsession`
        as the single source of truth for "what's running right now" (set by
        ``SubagentMixin._open_subsession``/``_close_subsession``).
        """
        active = self._transient.active_subsession
        if active is not None:
            self._transient.append_subsession_marker(str(active["subsession_id"]), marker)
        else:
            self._transient.append_marker(marker)

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
            self._session.awaiting_first_chunk = False
            await self._sink.send(Envelope.make_thinking_chunk(stream_id, event.text))
        elif isinstance(event, TokenDelta):
            self._session.awaiting_first_chunk = False
            await self._sink.send(Envelope.make_stream_chunk(stream_id, event.text))
        elif isinstance(event, ToolCallArgDelta):
            self._session.awaiting_first_chunk = False
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

    async def emit_usage(
        self, turn_end: TurnEnd, model: str, duration_seconds: float, agent_name: str
    ) -> None:
        """Push a per-call usage record (tokens, model, running cost), and
        persist it as a ``usage`` marker.

        The marker lets :class:`~._history.HistoryProjector` replay the same
        "Kodo responded in..." row on reload, in its correct chronological
        position — previously this was a live-only event with no persisted
        equivalent, so the WebView had to reconstruct it by keeping whatever
        had accumulated in memory and splicing it in after a fresh history
        load, which is what let entries drift out of order across a reload.

        Also carries this one call's own ``usd_cost`` (as opposed to the
        running ``cumulative_usd``), ``stop_reason``, and the ``agent_name``
        that made the call — this used to be written to a separate,
        never-read-back ``agents/<agent>.jsonl`` audit log
        (:meth:`~kodo.state.TransientStore.write_agent_record`, now removed);
        folding it into this marker keeps everything visible in the feed in
        the one file (session or subsession) it actually belongs to.

        Args:
            turn_end (TurnEnd): The completed call's usage/stop-reason event.
            model (str): Model identifier used for this call.
            duration_seconds (float): Wall-clock duration of this call.
            agent_name (str): The agent that made this call (main entry agent
                or sub-agent) — audit/display only.
        """
        payload = {
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
            "usd_cost": round(turn_end.usage.usd_cost, 6),
            "stop_reason": turn_end.stop_reason,
            "agent": agent_name,
        }
        self._append_marker({"type": "usage", **payload})
        await self._sink.send(Envelope.make_event(EVT_USAGE_UPDATE, payload))

    async def emit_session_naming(self, active: bool) -> None:
        """Tell the client whether the silent session-titler call is running.

        Drives a transient "Naming session …" indicator in the WebView so the
        titling round-trip (which streams nothing) does not look like a stall.
        """
        await self._sink.send(Envelope.make_event(EVT_SESSION_NAMING, {"active": active}))

    async def emit_web_search_note(self, tool_call_id: str, text: str) -> None:
        """Push one live narration note from the ``web_search`` agent's tool loop.

        Appended to the "Web Search is in progress" collapsible block
        (doc/WEB_SEARCH.md §6) as it runs; ``tool_call_id`` correlates it with
        the ``web_search`` call's ``agent.tool_call_prep`` card. Live-only —
        the durable copy is the sidecar file ``_run_web_search_agent`` writes
        via ``TransientStore.write_web_search_notes`` once the run ends.
        """
        await self._sink.send(
            Envelope.make_event(EVT_WEB_SEARCH_NOTE, {"tool_call_id": tool_call_id, "text": text})
        )

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
        """Push a user-facing runtime error, and persist it as a marker.

        The marker (``type: "error"``) lets :class:`~._history.HistoryProjector`
        replay the same error card on reload — previously this was a live-only
        event, so an error surfaced right before the user reloaded the WebView
        vanished for good even though it aborted the turn.
        """
        self._append_marker(
            {
                "type": "error",
                "message": message,
                "recoverable": recoverable,
            }
        )
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

    async def emit_security_rule_added(self, scope: str, executable: str, subcommand: str) -> None:
        """Push the user's own record of a just-granted "always allow" rule,
        and persist it as a marker.

        Fired by :meth:`WorkflowEngine.add_security_rule` /
        :meth:`~WorkflowEngine.add_security_path_rule` right after the rule is
        actually persisted (doc/SECURITY_RULES_PLAN.md §2.4/§2.7) — ``scope``
        is always ``"session"`` or ``"global"`` here (the caller already
        filtered out the unknown-scope no-op). The marker (``type:
        "security_rule_added"``) lets :class:`~._history.HistoryProjector`
        replay the same notice on reload, mirroring :meth:`emit_error`.
        """
        self._append_marker(
            {
                "type": "security_rule_added",
                "scope": scope,
                "executable": executable,
                "subcommand": subcommand,
            }
        )
        await self._sink.send(
            Envelope.make_event(
                EVT_SECURITY_RULE_ADDED,
                {"scope": scope, "executable": executable, "subcommand": subcommand},
            )
        )

    async def emit_agent_unstuck_nudge(self, note: str, reasons: list[str], mode: str) -> None:
        """Push the client-only explanation for a just-injected stuck-agent nudge.

        Fired right after the nudge is persisted (doc/STUCK_DETECTION.md,
        ``WatchdogMixin._persist_nudge`` and ``_run_entry_agent``'s deferred
        path) — the nudge's actual ``content`` is a real LLM-facing turn the
        agent's next streamed response follows on from, but the client never
        typed it and has no local echo, so this event (not the message
        content) is what the feed renders in its place.
        """
        await self._sink.send(
            Envelope.make_event(
                EVT_AGENT_UNSTUCK_NUDGE, {"note": note, "reasons": reasons, "mode": mode}
            )
        )

    async def emit_agent_stuck_critical(self, message: str) -> None:
        """Push a client-only notice that the stuck-agent watchdog gave up, and persist it.

        Fired when an entry-agent turn stalls for the *second* consecutive
        time since its last real response (doc/STUCK_DETECTION.md,
        ``WatchdogMixin._persist_stuck_critical``) — the first stall already
        got one nudge, and stalling again right after means nudging is not
        working. Unlike the nudge (a real LLM-facing continuation turn), this
        ends the turn: persisted as a marker (``type: "agent_stuck_critical"``)
        so :class:`~._history.HistoryProjector` replays the same notice on
        reload, mirroring :meth:`emit_error`.
        """
        self._append_marker(
            {
                "type": "agent_stuck_critical",
                "message": message,
            }
        )
        await self._sink.send(Envelope.make_event(EVT_AGENT_STUCK_CRITICAL, {"message": message}))

    async def emit_cyclic_thinking_notice(self, message: str) -> None:
        """Push the client-only rendering hint for a just-persisted cyclic-thinking notice.

        Fired right after the notice is persisted (doc/STUCK_DETECTION.md
        §2.7, ``WatchdogMixin._persist_cyclic_thinking_notice``) — mirrors
        :meth:`emit_agent_unstuck_nudge`: the notice's actual ``content`` is
        a real LLM-facing turn, but the client never typed it and has no
        local echo, so this event is what the feed renders in its place. No
        marker append here — persistence is via the ``kind``-tagged message
        itself, exactly like the ordinary nudge.
        """
        await self._sink.send(
            Envelope.make_event(EVT_AGENT_CYCLIC_THINKING_NOTICE, {"message": message})
        )

    async def emit_cyclic_thinking_critical(self, message: str) -> None:
        """Push+persist a client-only notice that a second cyclic-thinking loop ended the turn.

        Fired when the entry-agent's thinking hits a *second* detected
        repetition loop since its last real response
        (doc/STUCK_DETECTION.md §2.7,
        ``WatchdogMixin._persist_cyclic_thinking_critical``) — mirrors
        :meth:`emit_agent_stuck_critical`'s shape (a marker so
        :class:`~._history.HistoryProjector` replays it on reload) but is a
        distinct event/marker type, since the root cause and message differ
        from the ordinary stuck-agent critical notice.
        """
        self._append_marker(
            {
                "type": "agent_cyclic_thinking_critical",
                "message": message,
            }
        )
        await self._sink.send(
            Envelope.make_event(EVT_AGENT_CYCLIC_THINKING_CRITICAL, {"message": message})
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

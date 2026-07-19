"""Interrupted-turn handling: user Stop folding and cold-restart resume.

Every tool-calling main turn flushes its assistant ``tool_use`` to disk
before dispatch (see :mod:`._turns`), so an interrupted turn always leaves a
dangling assistant message. Cold-restart resume only safely *re-dispatches*
a dangling call from :data:`_RESUME_REDISPATCH_TOOLS` — or the single
dangling call, of any tool, that ``TransientStore.pending_security_alert``
proves was still waiting at the security gate and therefore never actually
dispatched (doc/SECURITY.md §7). Any other dangling tool call is reported
back to the model as interrupted rather than re-executed, since re-running an
arbitrary tool (a shell command, a file write, ...) could duplicate its side
effects.
"""

from __future__ import annotations

import json
import logging
import uuid

from kodo.common import Envelope
from kodo.llms import Message, ToolCallLogger
from kodo.tools import tools_for_agent

from ._proto import EngineHost
from ._shared import _GUIDE_AGENT_NAME

_log = logging.getLogger(__name__)

_SUBAGENT_SPAWNING_TOOLS = frozenset({"run_subagent", "run_author_critic_iteration"})

# Dangling tool calls that cold-restart resume re-dispatches for real. The two
# spawners are safe because their sub-agent replay ledger guarantees a
# completed subsession is never re-run. ``ask_user`` and ``escalate_blocker``
# are safe because their only "side effect" is asking the present user: the
# whole question batch is re-driven from scratch (partial answers are never
# stored anywhere), which is the required crash behaviour for it.
_RESUME_REDISPATCH_TOOLS = _SUBAGENT_SPAWNING_TOOLS | {"ask_user", "escalate_blocker"}

# Appended to session.jsonl (as a real, LLM-visible ``assistant`` message)
# whenever the user clicks Stop mid-turn — see ``stop``/``_persist_interrupted_turn``.
# This is the context-visible counterpart to the client-only, display-only
# ``<kodo_crit>`` callout the WebView renders for the human (SessionEntryView's
# ``interrupted`` case, tagged ``exclude_from_context`` and never sent here):
# that one tells the *user* what happened, this one tells the *model*.
_STOPPED_TURN_NOTICE = (
    "The ongoing session was interrupted by the user before this turn finished "
    "responding — anything above from this turn may be incomplete. I will not "
    "silently resume or retry it; I'll wait for the user's next message."
)


class ResumeMixin:
    """Folds Stops into the record and resumes interrupted turns on restart."""

    # Declared so the writes in _resume_main_turn don't let mypy infer a
    # narrower class attribute conflicting with the EngineHost/_core one.
    _replay_subsessions: list[dict[str, object]] | None

    def _has_dangling_tool_use(self: EngineHost) -> bool:
        """True when the last persisted main message awaits tool results.

        Every tool-calling turn now flushes the assistant ``tool_use`` to disk
        before dispatch (not just sub-agent spawns), so an interrupted turn
        leaves that assistant message as the final persisted main message with
        no following ``tool_result``. That is the marker of a resumable turn:
        :meth:`_resume_main_turn` re-dispatches sub-agent spawns via the
        replay ledger, re-drives ``ask_user`` batches from scratch, and
        reports any other pending call back to the model as interrupted
        (never re-executing it — its side effects may already have landed).
        """
        if not self._main_messages:
            return False
        last = self._main_messages[-1]
        if last.role != "assistant" or not isinstance(last.content, list):
            return False
        return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in last.content)

    def _persist_interrupted_turn(self: EngineHost, entry_agent: str) -> None:
        """Fold a user-initiated Stop into ``session.jsonl`` instead of losing it.

        Called from :meth:`~._core.WorkflowEngine.stop` right after the worker
        task is cancelled. Cancellation can land in one of two places, and both
        are already durable by the time we get here:

        * mid tool-dispatch — the main turn always flushes the assistant's
          ``tool_use`` message to disk *before* dispatching any tool (see
          ``flush_before_dispatch`` on ``_run_agent_turn``), so
          :meth:`_has_dangling_tool_use` finds it and this synthesizes the
          missing ``tool_result`` for each pending call, exactly like
          :meth:`_resume_main_turn` does for a cold restart.
        * mid LLM stream — ``_run_agent_turn``'s own ``CancelledError``
          handler already turned whatever text/thinking/tool_use had arrived
          into a persisted (possibly partial) assistant message before
          re-raising, so there is nothing further to flush here.

        Either way this appends one more, LLM-visible ``assistant`` message
        telling the agent the previous turn was cut short — see
        :data:`_STOPPED_TURN_NOTICE`.

        Unlike cold-restart resume, a live Stop never re-dispatches anything
        — not even a call ``pending_security_alert`` names — the same
        "I will not silently resume or retry it" rule applies to a call still
        sitting at the permission gate. Any such marker is still cleared here
        so it cannot outlive the dangling call it pointed at (which this
        method is folding into an ordinary interrupted result) and linger,
        unmatched, into a future resume.
        """
        if self._transient.pending_security_alert is not None:
            self._transient.update(pending_security_alert=None)
        if self._has_dangling_tool_use():
            last = self._main_messages[-1]
            assert isinstance(last.content, list)
            tool_uses = [
                b for b in last.content if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            tool_results = [
                self._interrupted_tool_result(str(b["id"]), str(b["name"]), reason="stopped")
                for b in tool_uses
            ]
            results_msg = Message(role="user", content=tool_results)
            self._main_messages = self._main_messages + [results_msg]
            self._transient.append_message(
                results_msg.role, results_msg.content, entry_agent=entry_agent
            )

        notice = Message(role="assistant", content=_STOPPED_TURN_NOTICE)
        self._main_messages = self._main_messages + [notice]
        # kind="stopped_notice" keeps this out of the LLM wire format (only
        # role/content round-trip into _main_messages) but lets the history
        # projector replay it as the same red "interrupted" callout the live
        # client shows, instead of a fake user-typed chat bubble.
        self._transient.append_message(
            notice.role, notice.content, entry_agent=entry_agent, kind="stopped_notice"
        )

    def _last_entry_agent(self: EngineHost) -> str:
        """Entry agent that produced the last persisted main message.

        Read from the ``entry_agent`` tag on the most recent message line in
        ``session.jsonl`` — *any* entry agent may have been holding the floor
        when the run was interrupted, so resume must not assume the Guide.
        Falls back to the Guide only for legacy/untagged sessions.
        """
        for line in reversed(self._transient.read_session_lines()):
            if "role" in line:
                ea = line.get("entry_agent")
                return ea if isinstance(ea, str) and ea else _GUIDE_AGENT_NAME
        return _GUIDE_AGENT_NAME

    async def _resume_main_turn(self: EngineHost) -> None:
        """Resume a main turn interrupted mid tool-dispatch after a restart.

        Every tool-calling turn flushes its assistant ``tool_use`` to disk
        before dispatch, so any interrupted turn leaves a dangling assistant
        message. Four cases, handled per pending call:

        * **Sub-agent spawns** (:data:`_SUBAGENT_SPAWNING_TOOLS`) are
          re-dispatched through the subsession replay ledger — a completed
          sub-session returns its stored result, the active one is rehydrated
          and driven to completion. Safe because the ledger never re-runs a
          finished sub-session.
        * **``ask_user``** is re-dispatched for real: the question batch is
          re-fired to the client from scratch. Anything the user had entered
          before the crash is deliberately not stored anywhere, so there is
          nothing to restore — they answer the whole batch again.
        * **The one call ``TransientStore.pending_security_alert`` names**, if
          any, is also re-dispatched for real: that marker proves the call
          was still waiting at the security gate — never handed to the tool —
          when the interruption happened, so re-dispatching it re-runs
          judgement fresh (picking up e.g. an "always allow" rule granted
          since) and, if still "ask", re-fires the exact same
          ``prompt.permission`` instead of a stub. See doc/SECURITY.md §7.
        * **Every other tool** (a shell command, a file write, ...) is *not*
          re-executed: its side effects may already have landed before the
          interruption, and there is no result ledger to dedupe against.
          Instead the model gets a synthesized ``interrupted`` result so the
          transcript stays well-formed and it can decide whether to retry.

        The entry agent is recovered from the persisted ``entry_agent`` tag, not
        assumed to be the Guide: any entry agent can be holding the floor at
        crash time.
        """
        last = self._main_messages[-1]
        if not isinstance(last.content, list):
            return
        tool_uses = [b for b in last.content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not tool_uses:
            return

        # New entry-agent turn — see the matching note in _run_entry_agent
        # (doc/STUCK_DETECTION.md).
        self._entry_turn_seq += 1

        # Claim the alert now, unconditionally: whether or not its id turns up
        # among tool_uses below (it always should), this resume pass is the
        # one deciding this call's fate, so the marker must not outlive it.
        alert_tool_call_id = self._transient.pending_security_alert
        if alert_tool_call_id is not None:
            self._transient.update(pending_security_alert=None)

        entry_agent = self._last_entry_agent()
        ledger = self._build_replay_ledger()
        self._replay_subsessions = ledger
        _log.info(
            "Resuming interrupted main turn for %r: %d pending tool call(s), "
            "%d subsession(s) to replay",
            entry_agent,
            len(tool_uses),
            len(ledger),
        )

        agent = self._registry.get(entry_agent, self._session.effective_autonomous)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)
        self._compactor.note_active_model(self._resolve_model_key(agent.capability))
        dispatcher = self._make_dispatcher(entry_agent, self._orch_session_id)
        tools = tools_for_agent(agent.tools)
        tool_desc = {t.name: t.user_description for t in tools}
        tool_logger = ToolCallLogger(self._llm_logs_dir())

        self._session.phase = "running"
        self._session.agent = entry_agent
        await self._emitters.emit_state()
        await self._emitters.emit_agent_started(entry_agent)

        # Preserve the model's original tool_use order: spawning calls and
        # ask_user are re-dispatched for real, all others get an interrupted
        # stand-in.
        tool_results: list[dict[str, object]] = []
        for b in tool_uses:
            tool_use_id = str(b["id"])
            tool_name = str(b["name"])
            raw_input = b.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            if tool_name in _RESUME_REDISPATCH_TOOLS or tool_use_id == alert_tool_call_id:
                spawned = await self._dispatch_tool_calls(
                    [(tool_use_id, tool_name, tool_input)],
                    dispatcher.dispatch,
                    tool_desc,
                    tool_logger,
                    entry_agent,
                )
                tool_results.extend(spawned)
            else:
                tool_results.append(self._interrupted_tool_result(tool_use_id, tool_name))
        self._replay_subsessions = None
        results_msg = Message(role="user", content=tool_results)
        self._main_messages = self._main_messages + [results_msg]
        self._transient.append_message(
            results_msg.role, results_msg.content, entry_agent=entry_agent
        )

        stream_id = uuid.uuid4().hex
        self._main_messages, _ = await self._run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=self._main_messages,
            tools=tools,
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=entry_agent,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=self._persist_main_messages(entry_agent),
            flush_before_dispatch=True,
            track_context=True,
            on_stall=self._make_stall_handler(
                agent_name=entry_agent, routing=routing, is_entry_turn=True
            ),
            on_tool_calls=self._make_progress_handler(is_entry_turn=True),
        )
        await self._sink.send(Envelope.make_stream_end(stream_id))
        await self._emitters.emit_agent_finished(entry_agent)
        if self._session.phase != "done":
            self._session.phase = "awaiting_user"
        self._session.agent = None
        await self._emitters.emit_state()
        await self._compactor.maybe_auto_compact()

    @staticmethod
    def _interrupted_tool_result(
        tool_use_id: str, tool_name: str, reason: str = "restart"
    ) -> dict[str, object]:
        """Stand-in ``tool_result`` for a non-spawn tool cut off mid-dispatch.

        Its side effects (a shell command, a file write, ...) may already have
        landed before the interruption and there is no result ledger to dedupe
        against, so it is never re-executed. The model instead sees an
        ``error`` envelope (rendered with a failure badge, and read back by
        :func:`tool_result_succeeded` as a failure) telling it the call did not
        complete and was not retried, so it can decide whether to re-issue it.

        Args:
            reason: ``"restart"`` for a cold crash-resume (see
                :meth:`_resume_main_turn`) or ``"stopped"`` for a live
                user-initiated Stop (see :meth:`_persist_interrupted_turn`) —
                selects the wording of the cause only.
        """
        cause = (
            "the session was interrupted (server restart or window reload)"
            if reason == "restart"
            else "the user clicked Stop"
        )
        payload = {
            "error": (
                f"The '{tool_name}' call did not complete before {cause} and was "
                "NOT re-executed to avoid duplicating side effects. Any changes it "
                "may have started are reflected in the workspace; re-issue the "
                "call if you still need it."
            )
        }
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(payload),
        }

    def _build_replay_ledger(self: EngineHost) -> list[dict[str, object]]:
        """Build the ordered subsession replay ledger from ``session.jsonl`` markers.

        Considers only the markers after the last persisted assistant message
        (the in-flight spawning turn). Each ``subsession_start`` becomes a ledger
        entry; one paired with a ``subsession_end`` is ``completed`` (its stored
        result is reused), an unpaired start is the single active subsession.
        """
        lines = self._transient.read_session_lines()
        last_assistant = -1
        for i, ln in enumerate(lines):
            if ln.get("role") == "assistant":
                last_assistant = i
        markers = [
            ln
            for ln in lines[last_assistant + 1 :]
            if ln.get("type") in ("subsession_start", "subsession_end")
        ]
        ends = {str(m["subsession_id"]): m for m in markers if m.get("type") == "subsession_end"}
        ledger: list[dict[str, object]] = []
        for m in markers:
            if m.get("type") != "subsession_start":
                continue
            sid = str(m["subsession_id"])
            end = ends.get(sid)
            end_result = end.get("result") if end else None
            # Preserve the stored result faithfully — the standard
            # return_result dict shape is reused verbatim by
            # _replay_next_subsession; a bare list is an older marker shape
            # some callers still tolerate. An active, un-closed subsession
            # carries no reusable result.
            result: object
            if isinstance(end_result, dict):
                result = dict(end_result)
            elif isinstance(end_result, list):
                result = list(end_result)
            else:
                result = {}
            ledger.append(
                {
                    "subsession_id": sid,
                    "agent": m.get("agent"),
                    "completed": end is not None,
                    "result": result,
                }
            )
        return ledger

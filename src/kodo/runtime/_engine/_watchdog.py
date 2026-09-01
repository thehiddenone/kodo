"""Stuck-agent detection and remediation (doc/STUCK_DETECTION.md).

Some model turns end without actually finishing the task — most commonly a
local model whose final call produces no tool call *and* no visible text
(the ``"(no text)"`` sentinel in :mod:`._turns`), one truncated by the
output-token cap mid-generation, or one that boils down to at most one word
("Done.") once punctuation is stripped. Left alone, an entry-agent turn like
this just goes idle (``session.phase == "awaiting_user"``) with the task
unfinished and no explanation, and a sub-agent turn like this hands its
parent a near-empty ``return_result`` fallback.

Detection is a small, explicit registry of independent checks (:data:`_DETECTORS`)
run against one :class:`TurnSignal` — add a new red flag by writing one more
``TurnSignal -> RedFlag | None`` function and appending it to the tuple, no
other wiring required.

Remediation is governed by the ``stuck_detection`` settings block
(``kodo/server/_config.py``, doc/SETTINGS.md): ``active`` gates by model
residence, ``scope`` by entry-agent-only vs. entry-agent-and-sub-agents, and
``auto_unstuck_interactive`` picks (outside autonomous mode) between nudging
immediately and asking first via the ``prompt.stuck_alert`` gate
(:meth:`~.._gates.GateOrchestrator.fire_stuck_alert`). Autonomous mode always
nudges immediately. An entry-agent turn only ever gets one nudge per streak
(:data:`WatchdogMixin._stuck_streak`, cleared on the next genuine response
*or* the next round that actually produces a tool call — a round that calls
a tool is evidence of progress even if the stall that preceded it was never
explained, so it gets the same "streak is over" treatment as a real final
response; see :meth:`WatchdogMixin._make_progress_handler`): if it stalls
again right after with no real progress in between, the turn ends with a
client-only "gave up" notice (:meth:`WatchdogMixin._persist_stuck_critical`)
instead of nudging or asking a second time.

:class:`WatchdogMixin` builds one ``on_stall`` closure and one
``on_tool_calls`` closure per turn (:meth:`WatchdogMixin._make_stall_handler`,
:meth:`WatchdogMixin._make_progress_handler`), threaded into
:meth:`~._turns.TurnLoopMixin._run_agent_turn` at every call site
(:mod:`._turns`, :mod:`._resume`, :mod:`._subagents`). The closures — not
``_run_agent_turn`` — own every stuck-specific decision, so the shared turn
loop stays completely agnostic of settings, gates, and the worker queue; it
only ever sees a ``TurnSignal -> StallDecision`` function and a plain
no-argument callback.

Every course-correction this module sends the model — an ordinary stall
nudge, the missing-``return_result`` reminder, or either of the two
mid-stream notices below — is one :class:`~._shared.Nudge`: one message,
two audiences (the LLM-visible ``llm_text`` that gets persisted and replayed
into context, and the client-only ``ui_text`` the transcript renders).
:meth:`WatchdogMixin._persist_nudge` is the single place that turns a
``Nudge`` into a persisted, ``kind="nudge"`` message and a live
``EVT_NUDGE``/``agent.nudge`` event — every closure below builds a ``Nudge``
and calls it, rather than persisting/emitting anything itself.

Three more failure modes get their own closures in this same module, all
built the same way as ``on_stall``/``on_tool_calls`` and threaded through the
same three call sites:

- :mod:`kodo.runtime._cyclic_thinking` detects a local model's *thinking*
  block degenerating into a repetition loop (the same few lines, or a
  near-duplicate of them, generated over and over) *while it is still
  streaming*, rather than waiting for the round to end — by the time an
  ordinary stall would be noticed, a runaway loop has already spent its
  whole thinking-token budget. :meth:`WatchdogMixin._make_cyclic_thinking_handler`
  builds the ``on_cyclic_thinking`` closure gated by the exact same
  ``stuck_detection`` settings (no second settings surface), with its own
  dedicated streak (:data:`WatchdogMixin._cycle_streak`, deliberately
  separate from :data:`_stuck_streak` so the two failure modes don't combine
  to trip either one's two-strike cap).
- :mod:`kodo.runtime._think_tag_guard` detects a literal ``<think>`` tag
  appearing inside a *tool call's arguments* as they stream — a model
  narrating its reasoning (or, worse, degenerating into a repetition loop)
  inside structured tool-call data instead of a real thinking block. This is
  never valid output, so :meth:`WatchdogMixin._make_think_in_tool_call_handler`
  is the one closure in this module that is **not** gated by
  ``stuck_detection`` settings at all — a hard protocol violation, not a
  stall heuristic (mirrors §2.8's missing-``return_result`` gate). Own
  streak, :data:`WatchdogMixin._think_tag_streak`.
- The same tool-call-argument stream is also fed to a second, independent
  :class:`~kodo.runtime._cyclic_thinking.CyclicThinkingDetector` instance
  (that class has nothing thinking-specific about it — only its module does)
  to catch *repeated* tool-call-argument content, exactly the failure mode
  that motivated both of these: a model embedding a thinking block inside a
  ``run_subagent`` call's task text, then repeating the same sentence inside
  it forever. :meth:`WatchdogMixin._make_tool_call_cyclic_handler` is gated
  by ``stuck_detection`` settings like the thinking-block detector (this one
  *is* a heuristic). Own streak, :data:`WatchdogMixin._tool_call_cycle_streak`.

All three mid-stream detectors share one shape once a cycle/tag is found:
the stream is already dead and the bad content already generated, so
``auto_unstuck_interactive``/``fire_stuck_alert`` are never consulted —
remediation is always immediate, and escalation to a critical, client-only
"gave up" notice on a second consecutive hit
(:meth:`WatchdogMixin._persist_cyclic_thinking_critical`,
:meth:`WatchdogMixin._persist_think_in_tool_call_critical`,
:meth:`WatchdogMixin._persist_tool_call_cyclic_critical`) — never itself a
``Nudge``, since there is nothing left to feed back to a turn that is
ending. See doc/STUCK_DETECTION.md §2.7/§2.9/§2.10 for the full design.
"""

from __future__ import annotations

import asyncio
import logging
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kodo.llms import LLMRouting, Message
from kodo.tools import ToolDispatcher

from ._proto import EngineHost
from ._shared import Nudge, RedFlag, StallDecision, TurnSignal

__all__ = ["RedFlag", "StallDecision", "TurnSignal", "WatchdogMixin", "detect_red_flags"]

_log = logging.getLogger(__name__)

# How long an entry-agent turn's stall sits quietly (session already idle,
# chat input already usable) before the interactive alarm fires — long enough
# that a prompt already queued up behind this one gets a chance to start
# first (which cancels the alarm; see WatchdogMixin._schedule_entry_turn_alarm).
_ENTRY_TURN_ALARM_DELAY_S = 1.0

# Safety valve against a sub-agent that never recovers: caps how many times
# one sub-agent _run_agent_turn call will retry inline (autonomous/
# auto-unstuck immediate nudges, or repeated manual "Unstick it" clicks)
# before giving up and letting the turn end normally. Entry-agent scope does
# not use this counter at all — see _stuck_streak below, which caps entry-
# agent turns at exactly one nudge before escalating to a critical notice.
# Also reused, unmodified, as the sub-agent-scope cap for both mid-stream
# tool-call detectors below (§2.9/§2.10) — same safety-valve reasoning.
_MAX_CONSECUTIVE_NUDGES = 2

_NUDGE_LLM_TEXT = (
    "You stopped before finishing the task, without producing a final response "
    "or calling a tool. Continue from exactly where you left off."
)

# Sub-agent scope only (doc/STUCK_DETECTION.md §2.8): a subsession that ends
# with a clean, non-stalled final response (no _DETECTORS flag matched) but
# never called `return_result` at all has not actually finished its contract,
# no matter how good that response reads — this is the exact shape of the
# toolchain_builder incident that motivated this check (session 1785719012:
# the sub-agent did all its real work correctly, then just wrote a prose
# summary and stopped). Reuses the ordinary nudge machinery (_persist_nudge)
# with this one synthetic flag, so persistence/replay/rendering are identical
# to any other stall nudge; only the trigger and its own dedicated one-shot
# cap (`return_result_nudged` in `_make_stall_handler`, deliberately separate
# from `stall_count`/`_cycle_streak` — same reasoning as keeping those two
# apart) are new. Deliberately independent of `stuck_detection` settings
# entirely (unlike every other check in this module): this is a hard tool
# contract, not a stall heuristic, so it must not go quiet just because a user
# turned stuck-detection off or scoped it to entry-agent-only.
_MISSING_RETURN_RESULT_FLAG = RedFlag(
    code="missing_return_result",
    hint="it finished without calling `return_result`, a hard requirement for sub-agents",
)

# Unlike _NUDGE_LLM_TEXT's generic "keep going", this must actually name the
# missed tool — "continue from where you left off" tells a model that
# genuinely finished its work nothing useful to do differently.
_MISSING_RETURN_RESULT_LLM_TEXT = (
    "You finished without calling `return_result`. This is a hard requirement: "
    "the task is not done until you call it. Call `return_result` now with "
    "your final result."
)

# Strike 1 of the mid-stream cyclic-thinking detector (kodo.runtime._cyclic_thinking):
# a single Nudge whose llm_text and ui_text are the same first-person sentence — the
# LLM-visible course-correction the model reads back next round *is* the <kodo_warn>
# callout the user sees (source="cyclic_thinking"). First-person, assistant-voice: this
# is read back to the model as its own note, not a user instruction.
_CYCLIC_THINKING_NOTICE = (
    "I noticed my own reasoning had fallen into a repetitive loop, generating the "
    "same thoughts over and over, and stopped it before it could burn through the "
    "rest of my thinking budget. I will not continue down that line of reasoning — "
    "let me reconsider a different approach to this task."
)

# Strike 1 of the mid-stream tool-call-argument cyclic detector (§2.10) — same
# dual-role shape as _CYCLIC_THINKING_NOTICE above, distinct wording naming the
# actual channel (tool-call arguments, not a thinking block).
_TOOL_CALL_CYCLIC_NOTICE = (
    "I noticed my tool call's arguments had fallen into a repetitive loop, generating "
    "the same content over and over, and stopped it before it could burn through the "
    "rest of the turn. I will not continue down that line — let me regenerate this tool "
    "call's arguments from scratch."
)


def _think_in_tool_call_llm_text(tool_name: str) -> str:
    """LLM-visible text for the think-in-tool-call nudge (§2.9) — names the tool.

    Mirrors ``_MISSING_RETURN_RESULT_LLM_TEXT``'s specificity: a generic
    "don't do that" would not tell the model which call to redo.
    """
    return (
        "You are not allowed to think inside a tool call. Thinking happened inside your "
        f"`{tool_name}` call. Put any reasoning in your own thinking block, not inside "
        f"tool-call arguments, then call `{tool_name}` again with clean arguments."
    )


def _flag_empty_final_turn(signal: TurnSignal) -> RedFlag | None:
    """No tool call *and* no real text — the ``"(no text)"`` case.

    A legitimate task completion always says something; an empty final turn
    is never a real "I'm done" — it is the model's stream ending (naturally
    or truncated) before it produced anything, mid-task.
    """
    if signal.text.strip():
        return None
    return RedFlag(
        code="empty_final_turn",
        hint="its last turn ended with no tool call and no visible response",
    )


def _flag_truncated_generation(signal: TurnSignal) -> RedFlag | None:
    """The call was cut off by the output-token cap, not a natural stop.

    ``"max_tokens"`` is llama.cpp's ``"length"`` finish reason remapped
    (``kodo.llms.llamacpp._llama._map_finish_reason``) — the model was still
    generating (possibly mid-sentence, mid-plan) when its output budget ran
    out.
    """
    if signal.stop_reason != "max_tokens":
        return None
    return RedFlag(
        code="truncated_generation",
        hint="its last response was cut off before it finished (hit the output length limit)",
    )


_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)


def _flag_terse_final_response(signal: TurnSignal) -> RedFlag | None:
    """The visible text boils down to at most one word once punctuation is stripped.

    Strips punctuation and splits on whitespace — a real completion says at
    least two words; a reply that reduces to zero or one word ("Done.",
    "Yes.", "...") reads the same as an empty turn. Two words are accepted
    as a real (if brief) completion ("Sounds good.", "All set.").
    """
    words = [w for w in signal.text.translate(_PUNCTUATION_TABLE).split()]
    if len(words) > 1:
        return None
    return RedFlag(
        code="terse_final_response",
        hint="its last response was at most one word, not a real completion",
    )


# Extend this tuple to add a new red flag. Each detector is independent, sees
# the same TurnSignal, and returns at most one RedFlag; detect_red_flags runs
# every one of them and never short-circuits on the first match.
_DETECTORS: tuple[Callable[[TurnSignal], RedFlag | None], ...] = (
    _flag_empty_final_turn,
    _flag_truncated_generation,
    _flag_terse_final_response,
)


def detect_red_flags(signal: TurnSignal) -> list[RedFlag]:
    """Run every registered detector against *signal*; return every match."""
    return [flag for flag in (detector(signal) for detector in _DETECTORS) if flag is not None]


@dataclass(frozen=True)
class _StuckSettings:
    """Resolved ``stuck_detection`` settings (doc/SETTINGS.md)."""

    active: str
    scope: str
    auto_unstuck_interactive: bool

    def applies(self, *, residence: str, is_entry_turn: bool) -> bool:
        if self.active == "off":
            return False
        if self.active == "local_only" and residence != "local":
            return False
        return is_entry_turn or self.scope == "top_level_and_subagents"


def _stuck_settings(settings: dict[str, object]) -> _StuckSettings:
    """Parse the ``stuck_detection`` settings block, defensively.

    Mirrors ``_resolve_model_key``'s style: an unrecognised/missing value
    falls back to its documented default rather than raising, so a stale or
    hand-edited ``settings.json`` never breaks a turn.
    """
    raw = settings.get("stuck_detection")
    raw = raw if isinstance(raw, dict) else {}
    active = raw.get("active")
    scope = raw.get("scope")
    return _StuckSettings(
        active=active if active in ("off", "local_only", "local_and_cloud") else "local_only",
        scope=scope
        if scope in ("top_level", "top_level_and_subagents")
        else "top_level_and_subagents",
        auto_unstuck_interactive=bool(raw.get("auto_unstuck_interactive", False)),
    )


def _nudge_note(flags: list[RedFlag], display_name: str, mode: str) -> str:
    """User-facing (never LLM-facing) explanation attached to a nudge's ``ui_text``."""
    reasons = "; ".join(flag.hint for flag in flags)
    action = "continued it automatically" if mode == "auto" else "continued it, as you confirmed"
    return f"Kōdo noticed {display_name} appeared to stop mid-task ({reasons}) and {action}."


class WatchdogMixin:
    """Builds the per-turn stall-handling closure and drives its side effects."""

    _entry_turn_seq: int
    _stuck_watchdog_task: asyncio.Task[None] | None
    _stuck_streak: bool
    _cycle_streak: bool
    _think_tag_streak: bool
    _tool_call_cycle_streak: bool

    def _make_stall_handler(
        self: EngineHost,
        *,
        agent_name: str,
        routing: LLMRouting,
        is_entry_turn: bool,
        subsession_id: str | None = None,
        dispatcher: ToolDispatcher | None = None,
    ) -> Callable[[TurnSignal], Awaitable[StallDecision]]:
        """Build the ``on_stall`` callback for one ``_run_agent_turn`` call.

        All state (the consecutive-nudge counter, which turn this is) lives
        in this closure, freshly built per call — ``_run_agent_turn`` itself
        never needs to reset or reach into it.

        Args:
            agent_name: The agent whose turn this is (entry agent or
                sub-agent name).
            routing: This turn's resolved :class:`LLMRouting` — ``residence``
                gates the ``active`` setting.
            is_entry_turn: ``True`` for the shared main entry-agent turn
                (mirrors ``_run_agent_turn``'s own ``track_context``),
                ``False`` for a sub-agent subsession.
            subsession_id: The owning subsession id when ``is_entry_turn`` is
                ``False`` — routes the persisted nudge to the right log.
            dispatcher: The subsession's :class:`~kodo.tools.ToolDispatcher`
                when ``is_entry_turn`` is ``False`` — read-only here, used
                only to check ``returned_output`` for the missing-
                ``return_result`` gate (doc/STUCK_DETECTION.md §2.8). Never
                passed for an entry-agent turn, which has no such contract.
        """
        stall_count = 0
        return_result_nudged = False

        async def _end_or_nudge_missing_return_result() -> StallDecision:
            """One last ``return_result`` reminder before a sub-agent turn truly ends.

            Called in place of every ``StallDecision(retry=False)`` a
            sub-agent scope would otherwise return from ``_on_stall`` —
            whether that turn looked like a clean completion, stuck_detection
            doesn't apply, the generic stall retries are exhausted, or the
            user declined a manual "Unstick it". Regardless of *why* the turn
            is about to end, a sub-agent that never called ``return_result``
            has not met its contract, so it gets exactly one more nudge
            specifically about that — capped by ``return_result_nudged``,
            deliberately its own counter so it always fires exactly once, on
            top of whatever the generic stall machinery already tried. If it
            still hasn't called ``return_result`` the next time this closure
            runs, this falls through to an ordinary ``retry=False`` and
            ``_drive_subsession``'s existing ``{schema_compliance: False}``
            fallback correctly marks the subsession failed.
            """
            nonlocal return_result_nudged
            if (
                is_entry_turn
                or dispatcher is None
                or dispatcher.returned_output is not None
                or return_result_nudged
            ):
                return StallDecision(retry=False)
            return_result_nudged = True
            display_name = self._display_name(agent_name)
            nudge = Nudge(
                llm_text=_MISSING_RETURN_RESULT_LLM_TEXT,
                ui_text=_nudge_note([_MISSING_RETURN_RESULT_FLAG], display_name, "auto"),
                reasons=[_MISSING_RETURN_RESULT_FLAG.code],
                mode="auto",
                source="missing_return_result",
            )
            message = await self._persist_nudge(
                agent_name=agent_name, subsession_id=subsession_id, nudge=nudge, role="user"
            )
            return StallDecision(retry=True, message=message)

        async def _on_stall(signal: TurnSignal) -> StallDecision:
            nonlocal stall_count
            flags = detect_red_flags(signal)
            if not flags:
                if is_entry_turn:
                    # A genuine, non-stalled final response — whatever streak
                    # was building (if any) is over.
                    self._stuck_streak = False
                    self._cycle_streak = False
                    self._think_tag_streak = False
                    self._tool_call_cycle_streak = False
                return await _end_or_nudge_missing_return_result()
            cfg = _stuck_settings(self._get_settings())
            if not cfg.applies(residence=routing.residence, is_entry_turn=is_entry_turn):
                return await _end_or_nudge_missing_return_result()

            # Resolved only once a stall is actually going to be acted on —
            # every ordinary (non-stalled) turn skips the registry lookup
            # entirely.
            display_name = self._display_name(agent_name)

            if is_entry_turn:
                if self._stuck_streak:
                    # One nudge already went out since the last real response
                    # and this turn stalled again right after it (with no
                    # successful tool call in between — see
                    # _make_progress_handler) — nudging a second time has
                    # shown no sign of working, so stop here and tell the
                    # user why instead of asking (or trying) again.
                    # _stuck_streak stays set: only a genuine response or a
                    # successful tool-call round clears it, so a third/fourth/...
                    # consecutive stall surfaces this same notice again
                    # rather than nudging.
                    await self._persist_stuck_critical(
                        agent_name=agent_name, flags=flags, display_name=display_name
                    )
                    return StallDecision(retry=False)

                immediate = self._session.effective_autonomous or cfg.auto_unstuck_interactive
                if immediate:
                    self._stuck_streak = True
                    nudge = Nudge(
                        llm_text=_NUDGE_LLM_TEXT,
                        ui_text=_nudge_note(flags, display_name, "auto"),
                        reasons=[flag.code for flag in flags],
                        mode="auto",
                        source="stall",
                    )
                    message = await self._persist_nudge(
                        agent_name=agent_name, subsession_id=None, nudge=nudge, role="user"
                    )
                    return StallDecision(retry=True, message=message)

                # Deferred: the turn ends normally (session goes idle, input
                # stays usable) — remediation is a decoupled follow-up, not
                # an inline retry. _stuck_streak is set once the nudge
                # actually lands (_run_entry_agent's nudge_detail branch),
                # not merely offered — a dismissed alarm never sets it.
                self._schedule_entry_turn_alarm(agent_name, display_name, flags)
                return StallDecision(retry=False)

            # Sub-agent scope: capped at _MAX_CONSECUTIVE_NUDGES inline
            # retries per call, exactly as before — no cross-turn streak.
            if stall_count >= _MAX_CONSECUTIVE_NUDGES:
                return await _end_or_nudge_missing_return_result()

            immediate = self._session.effective_autonomous or cfg.auto_unstuck_interactive
            if immediate:
                stall_count += 1
                nudge = Nudge(
                    llm_text=_NUDGE_LLM_TEXT,
                    ui_text=_nudge_note(flags, display_name, "auto"),
                    reasons=[flag.code for flag in flags],
                    mode="auto",
                    source="stall",
                )
                message = await self._persist_nudge(
                    agent_name=agent_name, subsession_id=subsession_id, nudge=nudge, role="user"
                )
                return StallDecision(retry=True, message=message)

            # The parent turn is already blocked on this sub-agent's
            # completion (spinner already showing), so there is no "looks
            # idle" state to preserve — ask right now, inline, exactly like
            # an ordinary prompt.permission gate.
            response = await self._gate.fire_stuck_alert(
                agent_name=agent_name, display_name=display_name, reasons=[f.hint for f in flags]
            )
            if response.action != "unstick":
                return await _end_or_nudge_missing_return_result()
            stall_count += 1
            nudge = Nudge(
                llm_text=_NUDGE_LLM_TEXT,
                ui_text=_nudge_note(flags, display_name, "manual"),
                reasons=[flag.code for flag in flags],
                mode="manual",
                source="stall",
            )
            message = await self._persist_nudge(
                agent_name=agent_name, subsession_id=subsession_id, nudge=nudge, role="user"
            )
            return StallDecision(retry=True, message=message)

        return _on_stall

    def _make_progress_handler(
        self: EngineHost, *, is_entry_turn: bool
    ) -> Callable[[], None] | None:
        """Build the ``on_tool_calls`` callback for one ``_run_agent_turn`` call.

        Entry-agent scope only: a round that produces a real tool call is
        evidence the agent is not actually stuck, so it clears every
        entry-agent streak (:data:`_stuck_streak`, :data:`_cycle_streak`,
        :data:`_think_tag_streak`, :data:`_tool_call_cycle_streak`) exactly
        like a genuine no-tool-call final response does in ``_on_stall`` —
        without this, one early stall stays "armed" through any number of
        subsequent successful tool-call rounds, so an unrelated later stall
        goes straight to :meth:`_persist_stuck_critical` (or the equivalent
        critical for the other failure modes) instead of getting its own
        nudge. Sub-agent scope has no cross-turn streak (all four streaks
        are entry-agent-only, doc/STUCK_DETECTION.md §2.4a), so this returns
        ``None`` for it and ``_run_agent_turn`` simply skips the callback.
        """
        if not is_entry_turn:
            return None

        def _on_tool_calls() -> None:
            self._stuck_streak = False
            self._cycle_streak = False
            self._think_tag_streak = False
            self._tool_call_cycle_streak = False

        return _on_tool_calls

    async def _persist_nudge(
        self: EngineHost,
        *,
        agent_name: str,
        subsession_id: str | None,
        nudge: Nudge,
        role: str,
    ) -> Message:
        """Persist *nudge* as a real, LLM-visible turn with a client-only ``detail``.

        The single place every closure in this module (and
        :meth:`~._turns.TurnLoopMixin._run_entry_agent`'s deferred-nudge
        branch, which persists the queued-alarm case the same way, by hand)
        goes to turn a :class:`~._shared.Nudge` into a persisted message:
        ``detail`` (``kind="nudge"``) never reaches the LLM —
        ``load_main_messages``/subsession rehydration only ever reads
        ``role``/``content`` — but lets ``HistoryProjector`` render this line
        as a distinct feed entry instead of a plain chat bubble (mirrors
        ``kind="stopped_notice"``, doc/STATE_AND_LIFECYCLE.md §4.1). Also
        pushes :data:`~kodo.transport.EVT_NUDGE` live, since the client has
        no local echo for a turn it never typed.

        ``role`` matters: an ordinary stall/missing-``return_result`` nudge
        is a ``"user"`` turn the model responds to; the two mid-stream
        notices (cyclic-thinking, tool-call-cyclic) are ``"assistant"`` —
        first-person, read back as the model's own course-correction, not an
        instruction from someone else.
        """
        detail: dict[str, object] = {
            "ui_text": nudge.ui_text,
            "reasons": nudge.reasons,
            "mode": nudge.mode,
            "source": nudge.source,
        }
        _log.info(
            "Nudge (session=%s agent=%s source=%s mode=%s reasons=%s)",
            self._orch_session_id,
            agent_name,
            nudge.source,
            nudge.mode,
            nudge.reasons,
        )
        if subsession_id is not None:
            self._transient.append_subsession_message(
                subsession_id,
                role,
                nudge.llm_text,
                kind="nudge",
                detail=detail,
            )
        else:
            self._transient.append_message(
                role,
                nudge.llm_text,
                entry_agent=agent_name,
                kind="nudge",
                detail=detail,
            )
        await self._emitters.emit_nudge(nudge.ui_text, nudge.reasons, nudge.mode, nudge.source)
        return Message(role=role, content=nudge.llm_text)

    async def _persist_stuck_critical(
        self: EngineHost, *, agent_name: str, flags: list[RedFlag], display_name: str
    ) -> None:
        """End an entry-agent turn for good instead of nudging (or asking) again.

        Only reached once :data:`_stuck_streak` is already set — i.e. this is
        the *second* consecutive stall since the last real response (with no
        intervening successful tool call — see
        :meth:`_make_progress_handler`), so the one nudge already sent (auto
        or manual) did not get the model unstuck. Client-only, mirroring
        ``emit_error``: never fed back to the LLM, and — unlike the nudge —
        does not clear :data:`_stuck_streak` itself; only a genuine
        non-stalled response or a successful tool-call round does, so a
        third, fourth, ... consecutive stall keeps surfacing this same
        notice rather than nudging again.
        """
        reasons = "; ".join(flag.hint for flag in flags)
        message = (
            f"Kōdo already nudged {display_name} once, but it stalled again right "
            f"after ({reasons}). Ending the turn instead of trying again — you may "
            "need to rephrase the prompt or step in."
        )
        _log.warning(
            "Stuck-agent critical (session=%s agent=%s reasons=%s)",
            self._orch_session_id,
            agent_name,
            [flag.code for flag in flags],
        )
        await self._emitters.emit_agent_stuck_critical(message)

    def _schedule_entry_turn_alarm(
        self: EngineHost, agent_name: str, display_name: str, flags: list[RedFlag]
    ) -> None:
        """Background-watch an idle entry-agent turn; alarm the user if it stays idle.

        Runs decoupled from the turn that detected the stall (which has
        already ended normally by the time this fires). ``seq`` pins this
        watcher to the exact turn that triggered it: if a new prompt starts
        — or starts *and finishes* — before the delay or the gate resolves,
        ``_entry_turn_seq`` has moved on and this watcher quietly no-ops
        rather than alarming about a turn the user has already moved past.
        """
        seq = self._entry_turn_seq

        async def _watch() -> None:
            try:
                await asyncio.sleep(_ENTRY_TURN_ALARM_DELAY_S)
            except asyncio.CancelledError:
                return
            if self._entry_turn_seq != seq or self._session.phase != "awaiting_user":
                return
            try:
                response = await self._gate.fire_stuck_alert(
                    agent_name=agent_name,
                    display_name=display_name,
                    reasons=[f.hint for f in flags],
                )
            except asyncio.CancelledError:
                return
            except Exception:
                _log.exception("Stuck-alert gate failed for agent=%s", agent_name)
                return
            if response.action != "unstick":
                return
            if self._entry_turn_seq != seq or self._session.phase != "awaiting_user":
                return
            # Same shape _persist_nudge would build (doc/STUCK_DETECTION.md
            # §2.5) — but this crosses the worker-queue boundary as a plain
            # dict (`Envelope`s aren't picklable across that boundary; see
            # _turns.py's `_run_entry_agent`, which persists it once the
            # queued prompt is actually processed) rather than a `Nudge`
            # object.
            detail = {
                "ui_text": _nudge_note(flags, display_name, "manual"),
                "reasons": [flag.code for flag in flags],
                "mode": "manual",
                "source": "stall",
            }
            await self._queue.put(
                {"text": _NUDGE_LLM_TEXT, "attachments": [], "nudge_detail": detail}
            )

        # Held on self so asyncio never garbage-collects it mid-sleep (a bare
        # fire-and-forget create_task is only weakly referenced); overwriting
        # a still-running previous watcher here is harmless — it is stale by
        # construction (a new stall only schedules once the prior turn ended)
        # and will simply no-op on its own _entry_turn_seq check.
        self._stuck_watchdog_task = asyncio.create_task(_watch(), name="kodo-stuck-watchdog")

    # ------------------------------------------------------------------
    # Mid-stream cyclic-thinking detector (kodo.runtime._cyclic_thinking)
    # ------------------------------------------------------------------

    def _make_cyclic_thinking_handler(
        self: EngineHost,
        *,
        agent_name: str,
        routing: LLMRouting,
        is_entry_turn: bool,
        subsession_id: str | None = None,
    ) -> Callable[[str], Awaitable[StallDecision]] | None:
        """Build the ``on_cyclic_thinking`` callback for one ``_run_agent_turn`` call.

        Gated by the exact same ``stuck_detection`` settings block as
        ordinary stalls (``active``/``scope``, doc/SETTINGS.md) — reused
        unchanged rather than introducing a second settings surface. Returns
        ``None`` when disabled or out of scope for this turn, so
        :meth:`~._turns.TurnLoopMixin._run_agent_turn` never even
        instantiates a detector for it — zero overhead for a turn this
        doesn't apply to.

        Deliberately simpler than :meth:`_make_stall_handler`:
        ``auto_unstuck_interactive`` and the
        :meth:`~.._gates.GateOrchestrator.fire_stuck_alert` ask-first gate
        are never consulted here. By the time this callback fires, the
        stream has already been cancelled mid-round and the repeated
        content already generated — there is nothing left to defer or ask
        about, so remediation is always immediate, for both strikes and
        both scopes.

        Args:
            agent_name: The agent whose round this is.
            routing: This turn's resolved :class:`LLMRouting` — ``residence``
                gates the ``active`` setting, exactly like ordinary stalls.
            is_entry_turn: ``True`` for the shared main entry-agent turn,
                ``False`` for a sub-agent subsession.
            subsession_id: The owning subsession id when ``is_entry_turn`` is
                ``False`` — routes the persisted notice to the right log.
        """
        cfg = _stuck_settings(self._get_settings())
        if not cfg.applies(residence=routing.residence, is_entry_turn=is_entry_turn):
            return None

        cycle_stall_count = 0  # sub-agent scope only; local to this one call

        async def _on_cyclic_thinking(thinking_excerpt: str) -> StallDecision:
            nonlocal cycle_stall_count
            preview = thinking_excerpt[-200:]

            if is_entry_turn:
                if self._cycle_streak:
                    await self._persist_cyclic_thinking_critical(
                        agent_name=agent_name,
                        display_name=self._display_name(agent_name),
                        preview=preview,
                    )
                    return StallDecision(retry=False)
                # First strike: set the streak, nudge inline (retry=True) —
                # the caller's next round carries this as real context, one
                # more attempt at the same task with the reconsideration
                # note in hand, exactly like an ordinary nudge's retry.
                self._cycle_streak = True
                nudge = Nudge(
                    llm_text=_CYCLIC_THINKING_NOTICE,
                    ui_text=_CYCLIC_THINKING_NOTICE,
                    reasons=["cyclic_thinking"],
                    mode="auto",
                    source="cyclic_thinking",
                )
                message = await self._persist_nudge(
                    agent_name=agent_name, subsession_id=None, nudge=nudge, role="assistant"
                )
                return StallDecision(retry=True, message=message)

            # Sub-agent scope: capped local counter, inline retry, silent end
            # on the cap — mirrors _make_stall_handler's sub-agent path, but
            # (per the docstring above) with no ask-first gate at all.
            if cycle_stall_count >= _MAX_CONSECUTIVE_NUDGES:
                return StallDecision(retry=False)
            cycle_stall_count += 1
            nudge = Nudge(
                llm_text=_CYCLIC_THINKING_NOTICE,
                ui_text=_CYCLIC_THINKING_NOTICE,
                reasons=["cyclic_thinking"],
                mode="auto",
                source="cyclic_thinking",
            )
            message = await self._persist_nudge(
                agent_name=agent_name, subsession_id=subsession_id, nudge=nudge, role="assistant"
            )
            return StallDecision(retry=True, message=message)

        return _on_cyclic_thinking

    async def _persist_cyclic_thinking_critical(
        self: EngineHost, *, agent_name: str, display_name: str, preview: str
    ) -> None:
        """End an entry-agent turn for good after a *second* cyclic-thinking hit.

        Only reached once :data:`_cycle_streak` is already set — the first
        hit's notice did not stop the model from looping again. Client-only,
        never fed back to the LLM (mirrors ``_persist_stuck_critical``, not
        the notice above) — the turn is ending, so there is nothing left for
        the model to read. Named and worded distinctly from
        ``_persist_stuck_critical``: this is a different, more specific root
        cause (a detected repetition loop, not an empty/terse/truncated
        reply), and the user-facing message should say so.
        """
        _log.warning(
            "Cyclic-thinking critical (session=%s agent=%s preview=%r)",
            self._orch_session_id,
            agent_name,
            preview,
        )
        message = (
            f"Kōdo detected {display_name}'s reasoning fall into a repetitive, "
            "hallucinated thinking loop a second time and stopped it again. Ending "
            "the turn instead of trying again — you may need to rephrase the prompt "
            "or step in."
        )
        await self._emitters.emit_cyclic_thinking_critical(message)

    # ------------------------------------------------------------------
    # Mid-stream think-in-tool-call detector (kodo.runtime._think_tag_guard)
    # ------------------------------------------------------------------

    def _make_think_in_tool_call_handler(
        self: EngineHost,
        *,
        agent_name: str,
        is_entry_turn: bool,
        subsession_id: str | None = None,
    ) -> Callable[[str], Awaitable[StallDecision]]:
        """Build the ``on_think_in_tool_call`` callback for one ``_run_agent_turn`` call.

        Unlike every other closure in this module, this one is **not** gated
        by ``stuck_detection`` settings at all (doc/STUCK_DETECTION.md §2.9)
        — a stray ``<think>`` tag inside tool-call arguments is never valid
        output, independent of model residence or whether a user turned
        stall heuristics off (mirrors §2.8's missing-``return_result`` gate:
        a hard contract violation, not a "might be stuck" heuristic a user
        might reasonably want a say in), so this always returns a real
        closure. In practice it only ever fires for local (llama.cpp) models
        anyway: the ``ToolCallArgDelta`` stream this reads (``_turns.py``)
        is only emitted by that plugin.

        Args:
            agent_name: The agent whose turn this is.
            is_entry_turn: ``True`` for the shared main entry-agent turn,
                ``False`` for a sub-agent subsession.
            subsession_id: The owning subsession id when ``is_entry_turn`` is
                ``False``.
        """
        tool_call_count = 0  # sub-agent scope only; local to this one call

        async def _on_think_in_tool_call(tool_name: str) -> StallDecision:
            nonlocal tool_call_count
            display_name = self._display_name(agent_name)

            def _nudge() -> Nudge:
                return Nudge(
                    llm_text=_think_in_tool_call_llm_text(tool_name),
                    ui_text=(
                        f"Kōdo noticed {display_name} tried to think inside a "
                        f"`{tool_name}` tool call and stopped it."
                    ),
                    reasons=["think_in_tool_call"],
                    mode="auto",
                    source="think_in_tool_call",
                )

            if is_entry_turn:
                if self._think_tag_streak:
                    await self._persist_think_in_tool_call_critical(
                        agent_name=agent_name, display_name=display_name, tool_name=tool_name
                    )
                    return StallDecision(retry=False)
                self._think_tag_streak = True
                message = await self._persist_nudge(
                    agent_name=agent_name, subsession_id=None, nudge=_nudge(), role="user"
                )
                return StallDecision(retry=True, message=message)

            # Sub-agent scope: capped local counter, inline retry, silent end
            # on the cap — mirrors _make_cyclic_thinking_handler's sub-agent
            # path, with no ask-first gate at all (see class docstring).
            if tool_call_count >= _MAX_CONSECUTIVE_NUDGES:
                return StallDecision(retry=False)
            tool_call_count += 1
            message = await self._persist_nudge(
                agent_name=agent_name, subsession_id=subsession_id, nudge=_nudge(), role="user"
            )
            return StallDecision(retry=True, message=message)

        return _on_think_in_tool_call

    async def _persist_think_in_tool_call_critical(
        self: EngineHost, *, agent_name: str, display_name: str, tool_name: str
    ) -> None:
        """End an entry-agent turn for good after a *second* think-in-tool-call hit.

        Only reached once :data:`_think_tag_streak` is already set. Client-only,
        never fed back to the LLM, mirroring ``_persist_stuck_critical``/
        ``_persist_cyclic_thinking_critical``.
        """
        _log.warning(
            "Think-in-tool-call critical (session=%s agent=%s tool=%s)",
            self._orch_session_id,
            agent_name,
            tool_name,
        )
        message = (
            f"Kōdo already told {display_name} once not to think inside a tool call, but "
            f"it did it again in a `{tool_name}` call. Ending the turn instead of trying "
            "again — you may need to rephrase the prompt or step in."
        )
        await self._emitters.emit_think_in_tool_call_critical(message)

    # ------------------------------------------------------------------
    # Mid-stream tool-call-argument cyclic detector (kodo.runtime._cyclic_thinking,
    # a second instance of the same detector class fed a different stream)
    # ------------------------------------------------------------------

    def _make_tool_call_cyclic_handler(
        self: EngineHost,
        *,
        agent_name: str,
        routing: LLMRouting,
        is_entry_turn: bool,
        subsession_id: str | None = None,
    ) -> Callable[[str], Awaitable[StallDecision]] | None:
        """Build the ``on_tool_call_cyclic`` callback for one ``_run_agent_turn`` call.

        Gated by the exact same ``stuck_detection`` settings as ordinary
        stalls and the thinking-block cyclic detector (doc/STUCK_DETECTION.md
        §2.10) — unlike :meth:`_make_think_in_tool_call_handler`, a repeated
        tool-call argument is a heuristic ("this looks like a loop"), not a
        hard protocol violation, so a user can reasonably turn it off.
        Otherwise identical in shape to :meth:`_make_cyclic_thinking_handler`:
        remediation is always immediate for both strikes and both scopes.
        """
        cfg = _stuck_settings(self._get_settings())
        if not cfg.applies(residence=routing.residence, is_entry_turn=is_entry_turn):
            return None

        cycle_stall_count = 0  # sub-agent scope only; local to this one call

        async def _on_tool_call_cyclic(preview: str) -> StallDecision:
            nonlocal cycle_stall_count
            preview = preview[-200:]

            if is_entry_turn:
                if self._tool_call_cycle_streak:
                    await self._persist_tool_call_cyclic_critical(
                        agent_name=agent_name,
                        display_name=self._display_name(agent_name),
                        preview=preview,
                    )
                    return StallDecision(retry=False)
                self._tool_call_cycle_streak = True
                nudge = Nudge(
                    llm_text=_TOOL_CALL_CYCLIC_NOTICE,
                    ui_text=_TOOL_CALL_CYCLIC_NOTICE,
                    reasons=["tool_call_cyclic"],
                    mode="auto",
                    source="tool_call_cyclic",
                )
                message = await self._persist_nudge(
                    agent_name=agent_name, subsession_id=None, nudge=nudge, role="assistant"
                )
                return StallDecision(retry=True, message=message)

            if cycle_stall_count >= _MAX_CONSECUTIVE_NUDGES:
                return StallDecision(retry=False)
            cycle_stall_count += 1
            nudge = Nudge(
                llm_text=_TOOL_CALL_CYCLIC_NOTICE,
                ui_text=_TOOL_CALL_CYCLIC_NOTICE,
                reasons=["tool_call_cyclic"],
                mode="auto",
                source="tool_call_cyclic",
            )
            message = await self._persist_nudge(
                agent_name=agent_name, subsession_id=subsession_id, nudge=nudge, role="assistant"
            )
            return StallDecision(retry=True, message=message)

        return _on_tool_call_cyclic

    async def _persist_tool_call_cyclic_critical(
        self: EngineHost, *, agent_name: str, display_name: str, preview: str
    ) -> None:
        """End an entry-agent turn for good after a *second* tool-call-repetition hit.

        Only reached once :data:`_tool_call_cycle_streak` is already set.
        Client-only, never fed back to the LLM, mirroring
        ``_persist_cyclic_thinking_critical``.
        """
        _log.warning(
            "Tool-call-cyclic critical (session=%s agent=%s preview=%r)",
            self._orch_session_id,
            agent_name,
            preview,
        )
        message = (
            f"Kōdo detected {display_name}'s tool-call arguments fall into a repetitive "
            "loop a second time and stopped it again. Ending the turn instead of trying "
            "again — you may need to rephrase the prompt or step in."
        )
        await self._emitters.emit_tool_call_cyclic_critical(message)

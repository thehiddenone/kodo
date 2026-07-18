"""Stuck-agent detection and remediation (doc/STUCK_DETECTION.md).

Some model turns end without actually finishing the task — most commonly a
local model whose final call produces no tool call *and* no visible text
(the ``"(no text)"`` sentinel in :mod:`._turns`), or one truncated by the
output-token cap mid-generation. Left alone, an entry-agent turn like this
just goes idle (``session.phase == "awaiting_user"``) with the task
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
nudges immediately.

:class:`WatchdogMixin` builds one ``on_stall`` closure per turn
(:meth:`WatchdogMixin._make_stall_handler`), threaded into
:meth:`~._turns.TurnLoopMixin._run_agent_turn` at every call site
(:mod:`._turns`, :mod:`._resume`, :mod:`._subagents`). The closure — not
``_run_agent_turn`` — owns every stuck-specific decision, so the shared turn
loop stays completely agnostic of settings, gates, and the worker queue; it
only ever sees a ``TurnSignal -> StallDecision`` function.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kodo.llms import LLMRouting, Message

from ._proto import EngineHost
from ._shared import RedFlag, StallDecision, TurnSignal

__all__ = ["RedFlag", "StallDecision", "TurnSignal", "WatchdogMixin", "detect_red_flags"]

_log = logging.getLogger(__name__)

# How long an entry-agent turn's stall sits quietly (session already idle,
# chat input already usable) before the interactive alarm fires — long enough
# that a prompt already queued up behind this one gets a chance to start
# first (which cancels the alarm; see WatchdogMixin._schedule_entry_turn_alarm).
_ENTRY_TURN_ALARM_DELAY_S = 5.0

# Safety valve against a model that never recovers: caps how many times one
# _run_agent_turn call will retry inline (autonomous/auto-unstuck immediate
# nudges, or repeated manual "Unstick it" clicks on one sub-agent turn)
# before giving up and letting the turn end normally. Does not apply to the
# entry-agent deferred/interactive path — each of those nudges is its own
# fresh _run_agent_turn call with its own new counter.
_MAX_CONSECUTIVE_NUDGES = 2

_NUDGE_LLM_TEXT = (
    "You stopped before finishing the task, without producing a final response "
    "or calling a tool. Continue from exactly where you left off."
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


# Extend this tuple to add a new red flag. Each detector is independent, sees
# the same TurnSignal, and returns at most one RedFlag; detect_red_flags runs
# every one of them and never short-circuits on the first match.
_DETECTORS: tuple[Callable[[TurnSignal], RedFlag | None], ...] = (
    _flag_empty_final_turn,
    _flag_truncated_generation,
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
        scope=scope if scope in ("top_level", "top_level_and_subagents") else "top_level",
        auto_unstuck_interactive=bool(raw.get("auto_unstuck_interactive", False)),
    )


def _nudge_note(flags: list[RedFlag], display_name: str, mode: str) -> str:
    """User-facing (never LLM-facing) explanation attached to a nudge's ``detail``."""
    reasons = "; ".join(flag.hint for flag in flags)
    action = "continued it automatically" if mode == "auto" else "continued it, as you confirmed"
    return f"Kōdo noticed {display_name} appeared to stop mid-task ({reasons}) and {action}."


class WatchdogMixin:
    """Builds the per-turn stall-handling closure and drives its side effects."""

    _entry_turn_seq: int
    _stuck_watchdog_task: asyncio.Task[None] | None

    def _make_stall_handler(
        self: EngineHost,
        *,
        agent_name: str,
        routing: LLMRouting,
        is_entry_turn: bool,
        subsession_id: str | None = None,
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
        """
        stall_count = 0

        async def _on_stall(signal: TurnSignal) -> StallDecision:
            nonlocal stall_count
            flags = detect_red_flags(signal)
            if not flags:
                return StallDecision(retry=False)
            cfg = _stuck_settings(self._get_settings())
            if not cfg.applies(residence=routing.residence, is_entry_turn=is_entry_turn):
                return StallDecision(retry=False)
            if stall_count >= _MAX_CONSECUTIVE_NUDGES:
                return StallDecision(retry=False)

            # Resolved only once a stall is actually going to be acted on —
            # every ordinary (non-stalled) turn skips the registry lookup
            # entirely.
            display_name = self._display_name(agent_name)
            immediate = self._session.effective_autonomous or cfg.auto_unstuck_interactive
            if immediate:
                stall_count += 1
                message = await self._persist_nudge(
                    agent_name=agent_name,
                    subsession_id=subsession_id,
                    flags=flags,
                    display_name=display_name,
                    mode="auto",
                )
                return StallDecision(retry=True, message=message)

            if is_entry_turn:
                # The turn ends normally (session goes idle, input stays
                # usable) — remediation is a decoupled follow-up, not an
                # inline retry. See _schedule_entry_turn_alarm.
                self._schedule_entry_turn_alarm(agent_name, display_name, flags)
                return StallDecision(retry=False)

            # Sub-agent scope: the parent turn is already blocked on this
            # sub-agent's completion (spinner already showing), so there is
            # no "looks idle" state to preserve — ask right now, inline,
            # exactly like an ordinary prompt.permission gate.
            response = await self._gate.fire_stuck_alert(
                agent_name=agent_name, display_name=display_name, reasons=[f.hint for f in flags]
            )
            if response.action != "unstick":
                return StallDecision(retry=False)
            stall_count += 1
            message = await self._persist_nudge(
                agent_name=agent_name,
                subsession_id=subsession_id,
                flags=flags,
                display_name=display_name,
                mode="manual",
            )
            return StallDecision(retry=True, message=message)

        return _on_stall

    async def _persist_nudge(
        self: EngineHost,
        *,
        agent_name: str,
        subsession_id: str | None,
        flags: list[RedFlag],
        display_name: str,
        mode: str,
    ) -> Message:
        """Persist the nudge as a real, LLM-visible ``user`` turn with a client-only ``detail``.

        ``detail`` (``kind="agent_unstuck_nudge"``) never reaches the LLM —
        ``load_main_messages``/subsession rehydration only ever reads
        ``role``/``content`` — but lets ``HistoryProjector`` render this line
        as a distinct feed entry instead of a plain chat bubble (mirrors
        ``kind="stopped_notice"``, doc/STATE_AND_LIFECYCLE.md §4.1). Also
        pushes :data:`~kodo.transport.EVT_AGENT_UNSTUCK_NUDGE` live, since the
        client has no local echo for a turn it never typed.
        """
        note = _nudge_note(flags, display_name, mode)
        detail: dict[str, object] = {
            "reasons": [flag.code for flag in flags],
            "note": note,
            "mode": mode,
        }
        if subsession_id is not None:
            self._transient.append_subsession_message(
                subsession_id,
                "user",
                _NUDGE_LLM_TEXT,
                kind="agent_unstuck_nudge",
                detail=detail,
            )
        else:
            self._transient.append_message(
                "user",
                _NUDGE_LLM_TEXT,
                entry_agent=agent_name,
                kind="agent_unstuck_nudge",
                detail=detail,
            )
        await self._emitters.emit_agent_unstuck_nudge(note, [flag.code for flag in flags], mode)
        return Message(role="user", content=_NUDGE_LLM_TEXT)

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
            detail = {
                "reasons": [flag.code for flag in flags],
                "note": _nudge_note(flags, display_name, "manual"),
                "mode": "manual",
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

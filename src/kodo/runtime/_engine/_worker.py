"""The engine's single worker coroutine — the only consumer of the task queue.

One ``asyncio.Queue`` + one worker (FR-WF-02): user prompts, manual
compaction requests, and config-change notifications are all funnelled
through the same queue so nothing ever races the in-flight turn.
"""

from __future__ import annotations

import asyncio
import logging

from kodo.common import Envelope
from kodo.llms import UnrecoverableError
from kodo.plan import PlanConflictError
from kodo.transport import EVT_API_KEY_REVOKE

from ._proto import EngineHost
from ._shared import _GUIDE_AGENT_NAME, _JUDGE_AGENT_NAME, _PROBLEM_SOLVER_AGENT_NAME

_log = logging.getLogger(__name__)


class WorkerMixin:
    """The queue-driven worker loop hosting every entry-agent run."""

    # Declared so the `= None` write below doesn't let mypy infer a bare-None
    # class attribute that conflicts with the EngineHost/_core declaration.
    _replay_subsessions: list[dict[str, object]] | None

    async def _run_worker(self: EngineHost) -> None:
        # Resume an interrupted sub-agent before accepting any queued prompt, so
        # the resume and a new prompt never drive _main_messages concurrently.
        if self._resume_subsession_pending:
            self._resume_subsession_pending = False
            self._freeze_effective_modes()
            try:
                await self._resume_main_turn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.exception("Failed to resume interrupted subsession: %s", exc)
                self._replay_subsessions = None
                self._session.agent = None
                await self._emitters.emit_error(str(exc), recoverable=True)
                await self._emitters.emit_state()

        while True:
            task = await self._queue.get()
            if task.get("kind") == "compact":
                try:
                    await self._compactor.run_manual_compaction()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.exception("Manual compaction failed: %s", exc)
                    await self._emitters.emit_error(f"Compaction failed: {exc}", recoverable=True)
                finally:
                    self._queue.task_done()
                continue
            if task.get("kind") == "config_changed":
                try:
                    await self._compactor.handle_config_changed()
                    await self._sync_thinking_level_to_model()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.exception("Config-change handling failed: %s", exc)
                finally:
                    self._queue.task_done()
                continue
            text = str(task.get("text", ""))
            raw_attachments = task.get("attachments", [])
            attachments = (
                [str(p) for p in raw_attachments] if isinstance(raw_attachments, list) else []
            )
            # Set only for the deferred half of a stuck-agent nudge
            # (doc/STUCK_DETECTION.md, WatchdogMixin._schedule_entry_turn_alarm)
            # — *text* is then the fixed continuation instruction, not
            # something the user typed.
            raw_nudge_detail = task.get("nudge_detail")
            nudge_detail = raw_nudge_detail if isinstance(raw_nudge_detail, dict) else None
            # Freeze every mode toggle for the whole prompt (guide + every
            # sub-agent it spawns). A toggle the user flips mid-prompt updates
            # the user-facing value but takes effect only when the next prompt
            # is dequeued here, so the in-flight prompt stays consistent end to
            # end and the client can tell "in effect" from "queued".
            self._freeze_effective_modes()
            try:
                # Name the session from its first prompt. Fire-and-forget: the
                # titler runs the local summarizer in a background thread and
                # reports the result whenever it lands (session.naming/
                # session.name), so it never delays the main agent's turn. A
                # nudge is never the "first prompt" worth titling from.
                if nudge_detail is None:
                    self._current_prompt_text = text
                    self._titler.maybe_generate_session_title(text)

                # The entry agent is chosen per prompt from the current
                # workflow mode: Problem Solver for "problem_solving", the
                # Guide (full Kodo pipeline) for "guided", and the validator-
                # only Judge for "judge" (never selected by kodo-vsix, which
                # only ever sends "guided"/"problem_solving").
                if self._session.workflow_mode == "problem_solving":
                    if self._agent_available(_PROBLEM_SOLVER_AGENT_NAME):
                        await self._run_problem_solver_with_input(
                            text, attachments, nudge_detail=nudge_detail
                        )
                    else:
                        await self._handle_input_no_agent(_PROBLEM_SOLVER_AGENT_NAME, text)
                elif self._session.workflow_mode == "judge":
                    if self._agent_available(_JUDGE_AGENT_NAME):
                        await self._run_judge_with_input(text, attachments)
                    else:
                        await self._handle_input_no_agent(_JUDGE_AGENT_NAME, text)
                elif self._agent_available(_GUIDE_AGENT_NAME):
                    await self._run_guide_with_input(text, attachments, nudge_detail=nudge_detail)
                else:
                    await self._handle_input_no_agent(_GUIDE_AGENT_NAME, text)

                if self._session.phase == "done":
                    _log.info("Project finalized — worker exiting")
                    break

            except asyncio.CancelledError:
                raise
            except PlanConflictError as exc:
                # A planner returned a new plan while the live one still had
                # unfinished tasks (doc/PLANNING.md §4). The agent has abandoned
                # work it already committed to, so there is nothing useful for it
                # to do next — the session stops rather than continuing against a
                # plan nobody is tracking. Phase "stopped", not "awaiting_user":
                # this is a deliberate halt, not a recoverable error the user
                # should just retry into.
                # No subsession recovery here, unlike the generic backstop below:
                # the conflict is raised *after* the planner's spawn returned, so
                # its subsession is already closed by the time this fires.
                _log.error("Plan conflict — stopping the session: %s", exc)
                await self._emitters.emit_plan_conflict_critical(str(exc))
                self._session.phase = "stopped"
                self._session.agent = None
                await self._emitters.emit_state()
            except UnrecoverableError as exc:
                _log.error("Unrecoverable LLM error (HTTP %d): %s", exc.status_code, exc)
                if exc.status_code == 401 and self._current_vendor:
                    _log.warning(
                        "API key rejected (401) for vendor=%r — sending revoke to client",
                        self._current_vendor,
                    )
                    await self._sink.send(
                        Envelope.make_event(EVT_API_KEY_REVOKE, {"vendor": self._current_vendor})
                    )
                await self._emitters.emit_error(str(exc), recoverable=False)
                self._session.phase = "stopped"
                self._session.agent = None
                await self._emitters.emit_state()
            except Exception as exc:
                _log.exception("Unhandled error in runtime worker: %s", exc)
                # Last-resort backstop for a crash that escaped every guard
                # closer to the failure (`_drive_subsession`'s and
                # `_spawn_subagent`'s) while a subsession was open. Runs before
                # the error notice so the client's subsession block closes
                # first, then the red callout lands outside it.
                crashed = await self._recover_crashed_subsession(exc)
                await self._emitters.emit_error(str(exc), recoverable=True)
                # Reset to an idle phase so the client unlocks its input and the
                # user can retry. Without this the phase stays "running" (set when
                # the turn began), leaving the webview's send box disabled and the
                # session wedged even though the worker is ready for the next
                # prompt. "awaiting_user" (not "stopped") avoids the client's
                # user-interrupt callout — this was an error, not a Stop.
                if self._session.phase != "done":
                    self._session.phase = "awaiting_user"
                self._session.agent = None
                await self._emitters.emit_state()
                if crashed:
                    self._enqueue_subsession_crash_report(crashed, exc)
            finally:
                self._queue.task_done()

    async def _recover_crashed_subsession(
        self: EngineHost, exc: BaseException
    ) -> dict[str, object] | None:
        """Close out a subsession a crash left open; return what it was, or ``None``.

        Reached only when an exception got past ``_drive_subsession``'s and
        ``_spawn_subagent``'s own guards — a sub-agent crash is normally
        converted into an escalation right where it happened, and the calling
        agent carries on inside the same turn. By the time it reaches here the
        turn is over and the calling agent's message history has an unanswered
        ``run_subagent`` tool call in it, so this half does the *bookkeeping*
        (:meth:`~._subagents.SubagentMixin._abort_active_subsession` writes the
        ``subsession_end`` marker, clears ``active_subsession``, resets the
        compactor gauge and pushes ``EVT_SUBSESSION_ENDED``) and
        :meth:`_enqueue_subsession_crash_report` does the *telling*.

        Returns the ``active_subsession`` record as it was before the abort, so
        the caller can name the agent in the crash report; ``None`` when no
        subsession was open (an ordinary entry-turn failure — nothing extra to
        do beyond the error notice the caller already sends).
        """
        active = self._transient.active_subsession
        if active is None:
            return None
        _log.error(
            "Subsession %s (%s) left open by an unhandled worker error — closing it out",
            active.get("subsession_id"),
            active.get("agent"),
        )
        try:
            await self._abort_active_subsession()
        except Exception:
            # Never let the recovery path itself wedge the worker; the marker
            # may be missing but the loop must keep serving prompts.
            _log.exception("Failed to close out crashed subsession")
        return dict(active)

    def _enqueue_subsession_crash_report(
        self: EngineHost, active: dict[str, object], exc: BaseException
    ) -> None:
        """Hand the crash back to the calling agent as its next turn.

        The calling agent asked for a sub-agent and — on this path — never got
        an answer, so it is queued a continuation that says exactly that and
        cites the exception. Queued rather than persisted directly:
        ``_run_entry_agent``'s ``nudge_detail`` branch is the established way a
        turn the user never typed enters the history (doc/STUCK_DETECTION.md
        §2.5), and going through the queue keeps this from racing the turn that
        just died.

        Guarded against a crash loop: only the *first* crash in a chain
        re-enters the agent. If the queued continuation crashes the same way,
        ``_subsession_crash_recovered`` is still set and the session simply
        goes idle with the error notice — a human decides what to do next
        rather than the engine spinning on a broken environment. The flag is
        cleared by :meth:`~._turns.TurnLoopMixin._run_entry_agent` on any turn
        that completes.
        """
        if self._subsession_crash_recovered:
            _log.warning("Subsession crash recovery already used this chain — not re-entering")
            return
        self._subsession_crash_recovered = True
        display = str(active.get("display_name") or active.get("agent") or "A sub-agent")
        detail = {
            "ui_text": (
                f"{display} crashed and its run was abandoned — control is back with you. "
                f"({type(exc).__name__}: {exc})"
            ),
            "reasons": ["subsession_crashed"],
            "mode": "auto",
            "source": "subsession_crash",
        }
        text = (
            f"The {display} sub-agent you invoked crashed before returning a result, and "
            f"its run was abandoned: {type(exc).__name__}: {exc}\n\n"
            "Nothing it may have written can be relied on. Decide what to do next: retry "
            "the stage, route around it, or — if this looks environmental (a missing "
            "directory, tool, or permission) — resolve that first, or ask the user."
        )
        self._queue.put_nowait({"text": text, "attachments": [], "nudge_detail": detail})

    async def _handle_input_no_agent(self: EngineHost, name: str, text: str) -> None:
        self._session.phase = "running"
        await self._emitters.emit_state()
        _log.warning(
            "Prompt received (len=%d) — entry agent %r not found; "
            "add subagent_%s.md to register one",
            len(text),
            name,
            name,
        )
        self._session.phase = "intake"
        await self._emitters.emit_state()

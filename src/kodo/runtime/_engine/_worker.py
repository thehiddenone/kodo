"""The engine's single worker coroutine — the only consumer of the task queue.

One ``asyncio.Queue`` + one worker (FR-WF-02): user prompts, manual
compaction requests, and config-change notifications are all funnelled
through the same queue so nothing ever races the in-flight turn.
"""

from __future__ import annotations

import asyncio
import logging

from kodo.common import Envelope
from kodo.llms.anthropic import UnrecoverableError
from kodo.transport import EVT_API_KEY_REVOKE

from ._proto import EngineHost
from ._shared import _GUIDE_AGENT_NAME, _PROBLEM_SOLVER_AGENT_NAME

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
                # session.name), so it never delays the main agent's turn.
                self._titler.maybe_generate_session_title(text)

                # The entry agent is chosen per prompt from the current
                # workflow mode: Problem Solver for "problem_solving", the
                # Guide (full Kodo pipeline) for "guided".
                if self._session.workflow_mode == "problem_solving":
                    if self._agent_available(_PROBLEM_SOLVER_AGENT_NAME):
                        await self._run_problem_solver_with_input(text, attachments)
                    else:
                        await self._handle_input_no_agent(_PROBLEM_SOLVER_AGENT_NAME, text)
                elif self._layout is None:
                    # Guided mode requires a bound project. The extension forces
                    # the picker before sending the first Guided prompt, so this
                    # is a safety net for an out-of-band prompt.
                    self._session.agent = None
                    await self._emitters.emit_error(
                        "Select a project before running Guided mode.", recoverable=True
                    )
                    await self._emitters.emit_state()
                elif self._agent_available(_GUIDE_AGENT_NAME):
                    await self._run_guide_with_input(text, attachments)
                else:
                    await self._handle_input_no_agent(_GUIDE_AGENT_NAME, text)

                if self._session.phase == "done":
                    _log.info("Project finalized — worker exiting")
                    break

            except asyncio.CancelledError:
                raise
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
            finally:
                self._queue.task_done()

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

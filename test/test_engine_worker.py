"""Tests for ``kodo.runtime._engine._worker`` — the single queue-driven
worker coroutine.

Drives ``WorkflowEngine._run_worker`` directly against a real
``asyncio.Queue`` with every collaborator it touches stubbed out (same
``object.__new__(WorkflowEngine)`` pattern as the rest of the engine test
suite), since standing up the real LLM/transport stack is out of scope for a
unit test.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from kodo.llms.anthropic import UnrecoverableError
from kodo.runtime import WorkflowEngine
from kodo.runtime._session import SessionState


class _FakeEmitters:
    def __init__(self) -> None:
        self.errors: list[tuple[str, bool]] = []
        self.state_emits = 0

    async def emit_error(self, message: str, *, recoverable: bool) -> None:
        self.errors.append((message, recoverable))

    async def emit_state(self) -> None:
        self.state_emits += 1


class _FakeCompactor:
    def __init__(
        self, *, compact_error: Exception | None = None, config_error: Exception | None = None
    ) -> None:
        self.compact_error = compact_error
        self.config_error = config_error
        self.manual_compaction_calls = 0
        self.config_changed_calls = 0

    async def run_manual_compaction(self) -> None:
        self.manual_compaction_calls += 1
        if self.compact_error is not None:
            raise self.compact_error

    async def handle_config_changed(self) -> None:
        self.config_changed_calls += 1
        if self.config_error is not None:
            raise self.config_error


class _FakeTitler:
    def __init__(self) -> None:
        self.titled: list[str] = []

    def maybe_generate_session_title(self, text: str) -> None:
        self.titled.append(text)


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


class _FakeTransient:
    """Only the one field the worker's subsession-crash backstop reads."""

    def __init__(self, active_subsession: dict[str, object] | None = None) -> None:
        self.active_subsession = active_subsession


def _make_engine(*, workflow_mode: str = "guided") -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._resume_subsession_pending = False
    engine._replay_subsessions = None
    engine._queue = asyncio.Queue()
    engine._session = SessionState(session_id="s1")
    engine._session.workflow_mode = workflow_mode
    engine._emitters = _FakeEmitters()
    engine._compactor = _FakeCompactor()
    engine._titler = _FakeTitler()
    engine._sink = _FakeSink()
    engine._current_vendor = None
    engine._transient = _FakeTransient()
    engine._subsession_crash_recovered = False
    engine.aborted_subsessions: list[dict[str, object] | None] = []

    async def _abort_active_subsession() -> None:
        engine.aborted_subsessions.append(engine._transient.active_subsession)
        engine._transient.active_subsession = None

    engine._abort_active_subsession = _abort_active_subsession
    engine._freeze_effective_modes = lambda: None
    engine._agent_available = lambda name: True
    engine.calls: list[tuple[str, str, list[str] | None]] = []
    # Every `nudge_detail` an entry-agent turn was invoked with, in order — the
    # client-only rendering half of a turn the user never typed.
    engine.nudge_details: list[dict[str, object]] = []

    def _recorder(label: str):
        async def _fn(
            text: str,
            attachments: list[str] | None = None,
            nudge_detail: dict[str, object] | None = None,
        ) -> None:
            engine.calls.append((label, text, attachments))
            if nudge_detail is not None:
                engine.nudge_details.append(nudge_detail)

        return _fn

    engine._run_guide_with_input = _recorder("guide")
    engine._run_problem_solver_with_input = _recorder("problem_solver")
    engine._run_judge_with_input = _recorder("judge")

    async def _handle_input_no_agent(name: str, text: str) -> None:
        engine.calls.append(("no_agent", name, None))

    engine._handle_input_no_agent = _handle_input_no_agent

    async def _resume_main_turn() -> None:
        engine.calls.append(("resume", "", None))

    engine._resume_main_turn = _resume_main_turn

    return engine


async def _drive(engine: WorkflowEngine, *, timeout: float = 0.3) -> None:
    """Run the worker until it idles (queue drained, still waiting) or exits."""
    task = asyncio.create_task(engine._run_worker())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Resume-on-start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_subsession_pending_runs_resume_before_queue() -> None:
    engine = _make_engine()
    engine._resume_subsession_pending = True

    await _drive(engine)

    assert engine.calls == [("resume", "", None)]
    assert engine._resume_subsession_pending is False


@pytest.mark.asyncio
async def test_resume_failure_is_recovered_and_worker_keeps_running() -> None:
    engine = _make_engine()
    engine._resume_subsession_pending = True
    engine._replay_subsessions = [{"subsession_id": "s1"}]
    engine._session.agent = "guide"

    async def _boom() -> None:
        raise RuntimeError("resume blew up")

    engine._resume_main_turn = _boom

    await _drive(engine)

    assert engine._emitters.errors == [("resume blew up", True)]
    assert engine._replay_subsessions is None
    assert engine._session.agent is None
    # Worker survives the failed resume and goes on to service the queue.
    engine._queue.put_nowait({"text": "hello"})
    await _drive(engine)
    assert ("guide", "hello", []) in engine.calls


# ---------------------------------------------------------------------------
# compact / config_changed control tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_task_runs_manual_compaction() -> None:
    engine = _make_engine()
    engine._queue.put_nowait({"kind": "compact"})

    await _drive(engine)

    assert engine._compactor.manual_compaction_calls == 1


@pytest.mark.asyncio
async def test_compact_task_error_is_recovered() -> None:
    engine = _make_engine()
    engine._compactor = _FakeCompactor(compact_error=RuntimeError("compaction failed"))
    engine._queue.put_nowait({"kind": "compact"})

    await _drive(engine)

    assert engine._emitters.errors == [("Compaction failed: compaction failed", True)]


@pytest.mark.asyncio
async def test_config_changed_task_runs_handler() -> None:
    engine = _make_engine()
    engine._queue.put_nowait({"kind": "config_changed"})

    await _drive(engine)

    assert engine._compactor.config_changed_calls == 1


@pytest.mark.asyncio
async def test_config_changed_task_error_is_recovered_without_emit_error() -> None:
    engine = _make_engine()
    engine._compactor = _FakeCompactor(config_error=RuntimeError("boom"))
    engine._queue.put_nowait({"kind": "config_changed"})

    await _drive(engine)

    # Config-change errors are only logged, never surfaced as an emit_error.
    assert engine._emitters.errors == []


# ---------------------------------------------------------------------------
# Prompt dispatch by workflow mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guided_prompt_runs_guide_when_available() -> None:
    engine = _make_engine(workflow_mode="guided")
    engine._queue.put_nowait({"text": "do the thing", "attachments": ["a.png"]})

    await _drive(engine)

    assert engine.calls == [("guide", "do the thing", ["a.png"])]
    assert engine._titler.titled == ["do the thing"]


@pytest.mark.asyncio
async def test_guided_prompt_falls_back_when_guide_unavailable() -> None:
    engine = _make_engine(workflow_mode="guided")
    engine._agent_available = lambda name: False
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert engine.calls == [("no_agent", "guide", None)]


@pytest.mark.asyncio
async def test_problem_solving_prompt_runs_problem_solver_when_available() -> None:
    engine = _make_engine(workflow_mode="problem_solving")
    engine._queue.put_nowait({"text": "fix it"})

    await _drive(engine)

    assert engine.calls == [("problem_solver", "fix it", [])]


@pytest.mark.asyncio
async def test_problem_solving_prompt_falls_back_when_unavailable() -> None:
    engine = _make_engine(workflow_mode="problem_solving")
    engine._agent_available = lambda name: False
    engine._queue.put_nowait({"text": "fix it"})

    await _drive(engine)

    assert engine.calls == [("no_agent", "problem_solver", None)]


@pytest.mark.asyncio
async def test_judge_prompt_runs_judge_when_available() -> None:
    engine = _make_engine(workflow_mode="judge")
    engine._queue.put_nowait({"text": "score it"})

    await _drive(engine)

    assert engine.calls == [("judge", "score it", [])]


@pytest.mark.asyncio
async def test_judge_prompt_falls_back_when_unavailable() -> None:
    engine = _make_engine(workflow_mode="judge")
    engine._agent_available = lambda name: False
    engine._queue.put_nowait({"text": "score it"})

    await _drive(engine)

    assert engine.calls == [("no_agent", "judge", None)]


@pytest.mark.asyncio
async def test_non_list_attachments_are_coerced_to_empty_list() -> None:
    engine = _make_engine(workflow_mode="guided")
    engine._queue.put_nowait({"text": "hi", "attachments": "not-a-list"})

    await _drive(engine)

    assert engine.calls == [("guide", "hi", [])]


# ---------------------------------------------------------------------------
# Phase "done" ends the worker loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_done_breaks_worker_loop() -> None:
    engine = _make_engine(workflow_mode="guided")

    async def _finish(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        engine.calls.append(("guide", text, attachments))
        engine._session.phase = "done"

    engine._run_guide_with_input = _finish
    engine._queue.put_nowait({"text": "wrap up"})

    task = asyncio.create_task(engine._run_worker())
    await asyncio.wait_for(task, timeout=0.3)  # must complete on its own, no cancel needed

    assert engine.calls == [("guide", "wrap up", [])]


# ---------------------------------------------------------------------------
# Error handling: UnrecoverableError and generic Exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrecoverable_401_error_revokes_key_and_stops_session() -> None:
    engine = _make_engine(workflow_mode="guided")
    engine._current_vendor = "anthropic"

    async def _fail(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        raise UnrecoverableError("bad key", 401)

    engine._run_guide_with_input = _fail
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert len(engine._sink.sent) == 1
    assert engine._sink.sent[0].payload == {"type": "api_key.revoke", "vendor": "anthropic"}
    assert engine._emitters.errors == [("bad key", False)]
    assert engine._session.phase == "stopped"
    assert engine._session.agent is None


@pytest.mark.asyncio
async def test_unrecoverable_non_401_error_does_not_revoke_key() -> None:
    engine = _make_engine(workflow_mode="guided")
    engine._current_vendor = "anthropic"

    async def _fail(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        raise UnrecoverableError("quota exceeded", 429)

    engine._run_guide_with_input = _fail
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert engine._sink.sent == []
    assert engine._emitters.errors == [("quota exceeded", False)]
    assert engine._session.phase == "stopped"


@pytest.mark.asyncio
async def test_generic_exception_resets_phase_to_awaiting_user() -> None:
    engine = _make_engine(workflow_mode="guided")

    async def _fail(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        raise ValueError("kaboom")

    engine._run_guide_with_input = _fail
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert engine._emitters.errors == [("kaboom", True)]
    assert engine._session.phase == "awaiting_user"
    assert engine._session.agent is None
    # No subsession was open, so the backstop does nothing beyond the notice.
    assert engine.aborted_subsessions == []
    assert engine._queue.empty()


def _crashing_engine(
    active: dict[str, object] | None, *, crash_every_turn: bool = False
) -> WorkflowEngine:
    """Engine whose guide turn raises, with *active* as the open subsession.

    By default only the *first* turn raises, so the queued recovery turn is
    observable in ``engine.calls`` — the realistic shape, and the one that
    proves the recovery terminates. ``crash_every_turn`` models an
    environmental failure that keeps reproducing.
    """
    engine = _make_engine(workflow_mode="guided")
    engine._transient = _FakeTransient(active)

    async def _abort_active_subsession() -> None:
        engine.aborted_subsessions.append(engine._transient.active_subsession)
        engine._transient.active_subsession = None

    engine._abort_active_subsession = _abort_active_subsession

    async def _fail(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        engine.calls.append(("guide", text, attachments))
        if nudge_detail is not None:
            engine.nudge_details.append(nudge_detail)
        if crash_every_turn or len(engine.calls) == 1:
            raise ValueError("kaboom")

    engine._run_guide_with_input = _fail
    return engine


@pytest.mark.asyncio
async def test_crash_with_open_subsession_closes_it_and_reports_to_caller() -> None:
    """A crash that escaped every closer guard must not leak the subsession.

    Before the backstop existed, `transient.active_subsession` stayed set, the
    client's collapsible block never closed, and the calling agent was told
    nothing at all — the original incident (a checkpoint commit raising
    FileNotFoundError after a sub-agent deleted its own project root).
    """
    active = {
        "subsession_id": "sub1",
        "agent": "toolchain_builder",
        "display_name": "Toolchain Builder",
    }
    engine = _crashing_engine(active)
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert engine.aborted_subsessions == [active]
    assert engine._transient.active_subsession is None
    # The human sees the exception via the ordinary recoverable-error notice
    # (kodo-vsix renders it as a red <kodo_crit> card plus a toast).
    assert engine._emitters.errors == [("kaboom", True)]
    # ...and the calling agent got a second, real turn naming the crash.
    assert [label for label, _text, _att in engine.calls] == ["guide", "guide"]
    recovery_text = engine.calls[1][1]
    assert "Toolchain Builder" in recovery_text
    assert "kaboom" in recovery_text
    assert engine._queue.empty()


@pytest.mark.asyncio
async def test_crash_recovery_turn_carries_a_subsession_crash_nudge_detail() -> None:
    """The recovery turn is a nudge, not a user prompt — so it renders as one.

    ``source`` must be ``"subsession_crash"``: kodo-vsix's reducer branches on
    it only to decide whether to flush a live mid-stream buffer, and this one
    needs no flush, which is why the new value costs no client change.
    """
    engine = _crashing_engine(
        {"subsession_id": "sub1", "agent": "toolchain_builder"}, crash_every_turn=True
    )
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    queued = engine.nudge_details
    assert len(queued) == 1
    detail = queued[0]
    assert detail["source"] == "subsession_crash"
    assert detail["reasons"] == ["subsession_crashed"]
    assert detail["mode"] == "auto"
    assert "toolchain_builder" in str(detail["ui_text"])
    assert "kaboom" in str(detail["ui_text"])


@pytest.mark.asyncio
async def test_crash_recovery_is_one_shot_per_chain() -> None:
    """A crash *while recovering from a crash* goes idle instead of looping."""
    engine = _crashing_engine({"subsession_id": "sub1", "agent": "developer"})
    engine._subsession_crash_recovered = True
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    # The subsession is still closed out — that half is never skipped.
    assert engine.aborted_subsessions == [{"subsession_id": "sub1", "agent": "developer"}]
    # But no second recovery turn is queued.
    assert engine._queue.empty()


@pytest.mark.asyncio
async def test_crash_recovery_survives_a_failing_abort() -> None:
    """The backstop must never wedge the worker, even if the abort itself fails."""
    engine = _crashing_engine({"subsession_id": "sub1", "agent": "developer"})

    async def _bad_abort() -> None:
        raise RuntimeError("marker write failed")

    engine._abort_active_subsession = _bad_abort
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert engine._session.phase == "awaiting_user"
    assert engine._emitters.errors == [("kaboom", True)]
    # Still reported to the caller — the abort failing does not lose the crash.
    assert [label for label, _text, _att in engine.calls] == ["guide", "guide"]


@pytest.mark.asyncio
async def test_resume_cancelled_error_propagates_uncaught() -> None:
    """A real cancellation mid-resume must not be swallowed as a plain error."""
    engine = _make_engine()
    engine._resume_subsession_pending = True

    async def _cancel() -> None:
        raise asyncio.CancelledError()

    engine._resume_main_turn = _cancel

    with pytest.raises(asyncio.CancelledError):
        await engine._run_worker()


@pytest.mark.asyncio
async def test_compact_cancelled_error_propagates_uncaught() -> None:
    engine = _make_engine()
    engine._compactor = _FakeCompactor(compact_error=asyncio.CancelledError())
    engine._queue.put_nowait({"kind": "compact"})

    with pytest.raises(asyncio.CancelledError):
        await engine._run_worker()


@pytest.mark.asyncio
async def test_config_changed_cancelled_error_propagates_uncaught() -> None:
    engine = _make_engine()
    engine._compactor = _FakeCompactor(config_error=asyncio.CancelledError())
    engine._queue.put_nowait({"kind": "config_changed"})

    with pytest.raises(asyncio.CancelledError):
        await engine._run_worker()


@pytest.mark.asyncio
async def test_prompt_cancelled_error_propagates_uncaught() -> None:
    engine = _make_engine(workflow_mode="guided")

    async def _cancel(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        raise asyncio.CancelledError()

    engine._run_guide_with_input = _cancel
    engine._queue.put_nowait({"text": "hi"})

    with pytest.raises(asyncio.CancelledError):
        await engine._run_worker()


# ---------------------------------------------------------------------------
# _handle_input_no_agent (the real method, unstubbed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_input_no_agent_cycles_phase_and_logs() -> None:
    engine = _make_engine()
    del engine._handle_input_no_agent  # use the real bound method

    await WorkflowEngine._handle_input_no_agent(engine, "guide", "hello there")

    assert engine._session.phase == "intake"
    assert engine._emitters.state_emits == 2


@pytest.mark.asyncio
async def test_generic_exception_after_phase_already_done_leaves_it_done() -> None:
    engine = _make_engine(workflow_mode="guided")

    async def _fail(
        text: str,
        attachments: list[str] | None = None,
        nudge_detail: dict[str, object] | None = None,
    ) -> None:
        engine._session.phase = "done"
        raise ValueError("kaboom after done")

    engine._run_guide_with_input = _fail
    engine._queue.put_nowait({"text": "hi"})

    await _drive(engine)

    assert engine._session.phase == "done"

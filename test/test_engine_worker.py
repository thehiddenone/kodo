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


def _make_engine(
    *, workflow_mode: str = "guided", layout: object | None = "bound"
) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._resume_subsession_pending = False
    engine._replay_subsessions = None
    engine._queue = asyncio.Queue()
    engine._session = SessionState(session_id="s1")
    engine._session.workflow_mode = workflow_mode
    engine._layout = layout
    engine._emitters = _FakeEmitters()
    engine._compactor = _FakeCompactor()
    engine._titler = _FakeTitler()
    engine._sink = _FakeSink()
    engine._current_vendor = None
    engine._freeze_effective_modes = lambda: None
    engine._agent_available = lambda name: True
    engine.calls: list[tuple[str, str, list[str] | None]] = []

    def _recorder(label: str):
        async def _fn(
            text: str,
            attachments: list[str] | None = None,
            nudge_detail: dict[str, object] | None = None,
        ) -> None:
            engine.calls.append((label, text, attachments))

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
async def test_guided_prompt_without_layout_emits_error_and_no_agent_call() -> None:
    engine = _make_engine(workflow_mode="guided", layout=None)
    engine._queue.put_nowait({"text": "do the thing"})

    await _drive(engine)

    assert engine.calls == []
    assert engine._emitters.errors == [("Select a project before running Guided mode.", True)]
    assert engine._session.agent is None


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

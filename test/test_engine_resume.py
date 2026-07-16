"""Tests for the rest of ``kodo.runtime._engine._resume`` not already covered
by ``test_engine_stop.py`` (which drives ``_persist_interrupted_turn``,
``_partial_assistant_message`` and ``_interrupted_tool_result``'s wording).

Here: ``_has_dangling_tool_use``, ``_last_entry_agent``,
``_build_replay_ledger``, and the cold-restart ``_resume_main_turn`` driver
itself, using the same ``object.__new__(WorkflowEngine)`` + minimal-stub
pattern as the rest of the engine test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms import Message
from kodo.runtime import WorkflowEngine
from kodo.runtime._session import SessionState

# ---------------------------------------------------------------------------
# _has_dangling_tool_use
# ---------------------------------------------------------------------------


def _bare_engine(*, main_messages: list[Message]) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._main_messages = main_messages
    return engine


def test_has_dangling_tool_use_false_when_no_messages() -> None:
    engine = _bare_engine(main_messages=[])
    assert engine._has_dangling_tool_use() is False


def test_has_dangling_tool_use_false_when_last_is_user() -> None:
    engine = _bare_engine(main_messages=[Message(role="user", content="hi")])
    assert engine._has_dangling_tool_use() is False


def test_has_dangling_tool_use_false_when_assistant_content_is_string() -> None:
    engine = _bare_engine(main_messages=[Message(role="assistant", content="plain text")])
    assert engine._has_dangling_tool_use() is False


def test_has_dangling_tool_use_false_when_no_tool_use_block() -> None:
    engine = _bare_engine(
        main_messages=[Message(role="assistant", content=[{"type": "text", "text": "hi"}])]
    )
    assert engine._has_dangling_tool_use() is False


def test_has_dangling_tool_use_true_when_tool_use_present() -> None:
    engine = _bare_engine(
        main_messages=[
            Message(
                role="assistant",
                content=[{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}}],
            )
        ]
    )
    assert engine._has_dangling_tool_use() is True


# ---------------------------------------------------------------------------
# _last_entry_agent
# ---------------------------------------------------------------------------


class _FakeTransientLines:
    def __init__(
        self,
        lines: list[dict[str, object]],
        pending_security_alert: str | None = None,
    ) -> None:
        self._lines = lines
        self.appended: list[tuple] = []
        self.pending_security_alert = pending_security_alert
        self.update_calls: list[dict[str, object]] = []

    def read_session_lines(self) -> list[dict[str, object]]:
        return self._lines

    def append_message(
        self,
        role: str,
        content: object,
        entry_agent: str | None = None,
        attachments: object = None,
        kind: str | None = None,
    ) -> None:
        self.appended.append((role, content, entry_agent, kind))

    def update(self, **kwargs: object) -> None:
        self.update_calls.append(kwargs)
        if "pending_security_alert" in kwargs:
            self.pending_security_alert = kwargs["pending_security_alert"]


def _engine_with_lines(lines: list[dict[str, object]]) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._transient = _FakeTransientLines(lines)
    return engine


def test_last_entry_agent_defaults_to_guide_when_no_lines() -> None:
    engine = _engine_with_lines([])
    assert engine._last_entry_agent() == "guide"


def test_last_entry_agent_reads_tag_from_most_recent_role_line() -> None:
    engine = _engine_with_lines(
        [
            {"role": "user", "content": "hi", "entry_agent": "guide"},
            {"role": "assistant", "content": "ok", "entry_agent": "problem_solver"},
        ]
    )
    assert engine._last_entry_agent() == "problem_solver"


def test_last_entry_agent_falls_back_when_tag_missing() -> None:
    engine = _engine_with_lines([{"role": "assistant", "content": "ok"}])
    assert engine._last_entry_agent() == "guide"


def test_last_entry_agent_skips_marker_only_lines() -> None:
    engine = _engine_with_lines(
        [
            {"role": "assistant", "content": "ok", "entry_agent": "problem_solver"},
            {"type": "subsession_start", "subsession_id": "s1"},
        ]
    )
    assert engine._last_entry_agent() == "problem_solver"


# ---------------------------------------------------------------------------
# _build_replay_ledger
# ---------------------------------------------------------------------------


def test_build_replay_ledger_empty_when_no_markers() -> None:
    engine = _engine_with_lines([{"role": "assistant", "content": "ok"}])
    assert engine._build_replay_ledger() == []


def test_build_replay_ledger_ignores_markers_before_last_assistant_message() -> None:
    lines = [
        {"type": "subsession_start", "subsession_id": "stale", "agent": "investigator"},
        {"role": "assistant", "content": "ok"},
    ]
    engine = _engine_with_lines(lines)
    assert engine._build_replay_ledger() == []


def test_build_replay_ledger_marks_completed_when_end_present() -> None:
    lines = [
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "investigator"},
        {"type": "subsession_end", "subsession_id": "s1", "result": {"summary": "done"}},
    ]
    engine = _engine_with_lines(lines)
    ledger = engine._build_replay_ledger()
    assert ledger == [
        {
            "subsession_id": "s1",
            "agent": "investigator",
            "completed": True,
            "result": {"summary": "done"},
        }
    ]


def test_build_replay_ledger_marks_active_when_no_end() -> None:
    lines = [
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "investigator"},
    ]
    engine = _engine_with_lines(lines)
    ledger = engine._build_replay_ledger()
    assert ledger == [
        {"subsession_id": "s1", "agent": "investigator", "completed": False, "result": {}}
    ]


def test_build_replay_ledger_preserves_list_result_shape() -> None:
    lines = [
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "investigator"},
        {"type": "subsession_end", "subsession_id": "s1", "result": ["a", "b"]},
    ]
    engine = _engine_with_lines(lines)
    ledger = engine._build_replay_ledger()
    assert ledger[0]["result"] == ["a", "b"]


def test_build_replay_ledger_multiple_subsessions_in_order() -> None:
    lines = [
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "investigator"},
        {"type": "subsession_end", "subsession_id": "s1", "result": {}},
        {"type": "subsession_start", "subsession_id": "s2", "agent": "planner"},
    ]
    engine = _engine_with_lines(lines)
    ledger = engine._build_replay_ledger()
    assert [entry["subsession_id"] for entry in ledger] == ["s1", "s2"]
    assert ledger[0]["completed"] is True
    assert ledger[1]["completed"] is False


# ---------------------------------------------------------------------------
# _resume_main_turn
# ---------------------------------------------------------------------------


class _FakeEmitters:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def emit_state(self) -> None:
        self.calls.append("emit_state")

    async def emit_agent_started(self, agent: str) -> None:
        self.calls.append(f"started:{agent}")

    async def emit_agent_finished(self, agent: str) -> None:
        self.calls.append(f"finished:{agent}")


class _FakeCompactor:
    def __init__(self) -> None:
        self.noted: list[str] = []
        self.auto_compact_calls = 0

    def note_active_model(self, key: str) -> None:
        self.noted.append(key)

    async def maybe_auto_compact(self) -> None:
        self.auto_compact_calls += 1


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


class _FakeDispatcher:
    stop_requested = False

    async def dispatch(
        self, name: str, args: dict[str, object], call_id: str, recovered: bool
    ) -> str:
        return "unused"


class _FakeRegistry:
    def get(self, name: str, autonomous: bool = False):
        from types import SimpleNamespace

        return SimpleNamespace(
            name=name, capability="medium", tools=frozenset(), system_prompt="sys prompt"
        )


def _resumable_engine(
    *,
    tool_uses: list[dict[str, object]],
    session_lines: list[dict[str, object]],
    tmp_path: Path,
    pending_security_alert: str | None = None,
) -> tuple[WorkflowEngine, _FakeCompactor, _FakeSink, list[tuple]]:
    engine = object.__new__(WorkflowEngine)
    engine._main_messages = [
        Message(role="user", content="go"),
        Message(role="assistant", content=tool_uses),
    ]
    engine._transient = _FakeTransientLines(
        session_lines, pending_security_alert=pending_security_alert
    )
    engine._registry = _FakeRegistry()
    engine._session = SessionState(session_id="s1")
    engine._session.effective_autonomous = False
    engine._compactor = _FakeCompactor()
    engine._emitters = _FakeEmitters()
    engine._sink = _FakeSink()
    engine._orch_session_id = "orch-1"

    async def _resolve_plugin(capability: str, force_model_key: str | None = None):
        from types import SimpleNamespace

        return (SimpleNamespace(name="fake-plugin"), "model-x", SimpleNamespace())

    engine._resolve_plugin = _resolve_plugin
    engine._resolve_model_key = lambda capability: f"key-{capability}"
    engine._make_dispatcher = lambda agent_name, session_id, deadline=None: _FakeDispatcher()
    engine._llm_logs_dir = lambda: tmp_path

    dispatch_calls: list[tuple] = []

    async def _dispatch_tool_calls(
        calls, tool_dispatch, tool_desc, tool_logger, agent_name, recovered_ids=None
    ):
        dispatch_calls.append((calls, agent_name))
        return [
            {"type": "tool_result", "tool_use_id": call[0], "content": "redispatched"}
            for call in calls
        ]

    engine._dispatch_tool_calls = _dispatch_tool_calls

    async def _run_agent_turn(**kwargs):
        return (engine._main_messages, [])

    engine._run_agent_turn = _run_agent_turn

    return engine, engine._compactor, engine._sink, dispatch_calls


@pytest.mark.asyncio
async def test_resume_main_turn_noop_when_last_content_not_list() -> None:
    engine = object.__new__(WorkflowEngine)
    engine._main_messages = [Message(role="assistant", content="plain text")]

    await engine._resume_main_turn()  # must return before touching anything else


@pytest.mark.asyncio
async def test_resume_main_turn_noop_when_no_tool_uses() -> None:
    engine = object.__new__(WorkflowEngine)
    engine._main_messages = [
        Message(role="assistant", content=[{"type": "text", "text": "partial"}])
    ]

    await engine._resume_main_turn()


@pytest.mark.asyncio
async def test_resume_main_turn_redispatches_ask_user_and_finishes(tmp_path: Path) -> None:
    tool_uses = [{"type": "tool_use", "id": "tu_1", "name": "ask_user", "input": {"q": "?"}}]
    engine, compactor, sink, dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[{"role": "assistant", "content": "ok", "entry_agent": "guide"}],
        tmp_path=tmp_path,
    )

    await engine._resume_main_turn()

    assert dispatch_calls[0][1] == "guide"
    assert dispatch_calls[0][0] == [("tu_1", "ask_user", {"q": "?"})]
    assert compactor.noted == ["key-medium"]
    assert compactor.auto_compact_calls == 1
    assert engine._session.phase == "awaiting_user"
    assert engine._session.agent is None
    assert engine._replay_subsessions is None
    assert any(env.kind == "stream_end" for env in sink.sent)


@pytest.mark.asyncio
async def test_resume_main_turn_does_not_redispatch_other_tools(tmp_path: Path) -> None:
    tool_uses = [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}}]
    engine, _compactor, _sink, dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[],
        tmp_path=tmp_path,
    )

    await engine._resume_main_turn()

    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_resume_main_turn_preserves_dangling_call_order(tmp_path: Path) -> None:
    tool_uses = [
        {"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}},
        {"type": "tool_use", "id": "tu_2", "name": "ask_user", "input": {}},
    ]
    engine, _compactor, _sink, dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[],
        tmp_path=tmp_path,
    )

    captured: list[list[object]] = []
    original_run_agent_turn = engine._run_agent_turn

    async def _capture_and_run(**kwargs):
        captured.append(kwargs["messages"])
        return await original_run_agent_turn(**kwargs)

    engine._run_agent_turn = _capture_and_run

    await engine._resume_main_turn()

    results_msg = captured[0][-1]
    ids = [block["tool_use_id"] for block in results_msg.content]
    assert ids == ["tu_1", "tu_2"]


# ---------------------------------------------------------------------------
# _resume_main_turn — pending_security_alert (dangling security alert)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_main_turn_redispatches_call_matching_pending_security_alert(
    tmp_path: Path,
) -> None:
    """A dangling run_command whose id matches pending_security_alert is
    provably still at the gate (never dispatched), so it is redispatched for
    real instead of stubbed — same treatment as ask_user/spawn tools."""
    tool_uses = [
        {"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {"command": "x"}}
    ]
    engine, _compactor, _sink, dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[],
        tmp_path=tmp_path,
        pending_security_alert="tu_1",
    )

    await engine._resume_main_turn()

    assert dispatch_calls == [([("tu_1", "run_command", {"command": "x"})], "guide")]


@pytest.mark.asyncio
async def test_resume_main_turn_clears_pending_security_alert_before_redispatch(
    tmp_path: Path,
) -> None:
    """The marker is claimed (cleared) up front, not left dangling if the
    redispatched call resolves without asking again (e.g. now allowed by a
    rule) — fire_permission only clears it when it actually re-fires."""
    tool_uses = [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}}]
    engine, _compactor, _sink, _dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[],
        tmp_path=tmp_path,
        pending_security_alert="tu_1",
    )

    await engine._resume_main_turn()

    assert engine._transient.pending_security_alert is None
    assert {"pending_security_alert": None} in engine._transient.update_calls


@pytest.mark.asyncio
async def test_resume_main_turn_does_not_redispatch_call_not_matching_alert(
    tmp_path: Path,
) -> None:
    """A stale/unrelated pending_security_alert (pointing at some other id)
    must not cause an unrelated dangling call to be redispatched."""
    tool_uses = [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}}]
    engine, _compactor, _sink, dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[],
        tmp_path=tmp_path,
        pending_security_alert="some-other-id",
    )

    await engine._resume_main_turn()

    assert dispatch_calls == []
    # Still claimed/cleared — this resume pass is the one deciding whatever
    # is dangling now, so a stale marker from an earlier turn must not persist.
    assert engine._transient.pending_security_alert is None


@pytest.mark.asyncio
async def test_resume_main_turn_only_alert_matched_call_redispatched_among_several(
    tmp_path: Path,
) -> None:
    """Only the specific tool_use_id the alert names is redispatched; a
    second dangling non-spawn call in the same batch still gets stubbed
    (unit-level check of the id-matching condition in isolation — dispatch is
    normally strictly sequential, so in practice the gating call is always
    the earliest dangling one, not a later one)."""
    tool_uses = [
        {"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}},
        {"type": "tool_use", "id": "tu_2", "name": "edit_file", "input": {}},
    ]
    engine, _compactor, _sink, dispatch_calls = _resumable_engine(
        tool_uses=tool_uses,
        session_lines=[],
        tmp_path=tmp_path,
        pending_security_alert="tu_1",
    )

    await engine._resume_main_turn()

    assert dispatch_calls == [([("tu_1", "run_command", {})], "guide")]

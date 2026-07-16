"""Unit tests for the small, self-contained engine collaborators:

- ``kodo.runtime._engine._shared`` — pure helper functions (project slugging).
- ``kodo.runtime._engine._services`` — ``_EngineServices``, a plain adapter
  that forwards each call to an injected callable.
- ``kodo.runtime._engine._events`` — ``EngineEmitters``, which turns engine
  state changes into outbound envelopes on a ``MessageSink`` plus a running
  cost total.

Each collaborator owns its own state (see ``_engine/__init__.py``'s module
docstring), so these are constructed directly with fake/minimal dependencies
rather than via the ``object.__new__(WorkflowEngine)`` pattern used for the
mixins in ``test_engine_stop.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms import (
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    TurnEnd,
    Usage,
)
from kodo.runtime._engine._events import EngineEmitters
from kodo.runtime._engine._services import _EngineServices
from kodo.runtime._engine._shared import _slugify_project_name, _unique_child_dir
from kodo.runtime._session import SessionState

# ---------------------------------------------------------------------------
# _shared._slugify_project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("My Project", "my-project"),
        ("  Trim Me  ", "trim-me"),
        ("weird!!!chars??here", "weird-chars-here"),
        ("Already-Slugged", "already-slugged"),
        ("123 Numbers", "123-numbers"),
        ("---", "project"),
        ("", "project"),
    ],
)
def test_slugify_project_name(name: str, expected: str) -> None:
    assert _slugify_project_name(name) == expected


# ---------------------------------------------------------------------------
# _shared._unique_child_dir
# ---------------------------------------------------------------------------


def test_unique_child_dir_returns_slug_when_free(tmp_path: Path) -> None:
    assert _unique_child_dir(tmp_path, "widget") == tmp_path / "widget"


def test_unique_child_dir_increments_suffix_on_collision(tmp_path: Path) -> None:
    (tmp_path / "widget").mkdir()
    (tmp_path / "widget-2").mkdir()

    assert _unique_child_dir(tmp_path, "widget") == tmp_path / "widget-3"


# ---------------------------------------------------------------------------
# _services._EngineServices
# ---------------------------------------------------------------------------


class _Recorder:
    """Records every call made through it, returning a canned value."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def make(self, name: str, retval: object = None):
        async def _fn(*args: object) -> object:
            self.calls.append((name, args))
            return retval

        return _fn


def _make_services(rec: _Recorder) -> _EngineServices:
    return _EngineServices(
        run_subagent=rec.make("run_subagent", {"ok": True}),
        run_dependency_manager=rec.make("run_dependency_manager", {"ok": True}),
        run_web_search_agent=rec.make("run_web_search_agent", {"ok": True}),
        run_author_critic=rec.make("run_author_critic", {"ok": True}),
        rollback=rec.make("rollback"),
        disable_autonomous=rec.make("disable_autonomous"),
        create_project=rec.make("create_project", {"root": "/tmp/proj"}),
        init_project=rec.make("init_project", {"root": "/tmp/existing"}),
        notify_tool_call_in_progress=rec.make("notify_tool_call_in_progress"),
        add_security_rule=rec.make("add_security_rule"),
    )


@pytest.mark.asyncio
async def test_engine_services_run_subagent_forwards_args() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    result = await services.run_subagent("guide", "investigator", {"task": "look"})

    assert result == {"ok": True}
    assert rec.calls == [("run_subagent", ("guide", "investigator", {"task": "look"}))]


@pytest.mark.asyncio
async def test_engine_services_run_dependency_manager_forwards_args() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    result = await services.run_dependency_manager({"action": "add"})

    assert result == {"ok": True}
    assert rec.calls == [("run_dependency_manager", ({"action": "add"},))]


@pytest.mark.asyncio
async def test_engine_services_run_web_search_agent_forwards_args() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    result = await services.run_web_search_agent({"query": "x"}, "tc_1")

    assert result == {"ok": True}
    assert rec.calls == [("run_web_search_agent", ({"query": "x"}, "tc_1"))]


@pytest.mark.asyncio
async def test_engine_services_run_author_critic_iteration_forwards_args() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    result = await services.run_author_critic_iteration(
        "guide", "author", "critic", "doc.md", {"spec": "spec.md"}, "polish it", True
    )

    assert result == {"ok": True}
    assert rec.calls == [
        (
            "run_author_critic",
            ("guide", "author", "critic", "doc.md", {"spec": "spec.md"}, "polish it", True),
        )
    ]


@pytest.mark.asyncio
async def test_engine_services_rollback_forwards_sha() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    await services.rollback("deadbeef")

    assert rec.calls == [("rollback", ("deadbeef",))]


@pytest.mark.asyncio
async def test_engine_services_disable_autonomous_mode() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    await services.disable_autonomous_mode()

    assert rec.calls == [("disable_autonomous", ())]


@pytest.mark.asyncio
async def test_engine_services_create_project_forwards_args_and_defaults() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    result = await services.create_project()

    assert result == {"root": "/tmp/proj"}
    assert rec.calls == [("create_project", ("", None, False))]


@pytest.mark.asyncio
async def test_engine_services_init_project_forwards_path() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    result = await services.init_project("/tmp/existing-project")

    assert result == {"root": "/tmp/existing"}
    assert rec.calls == [("init_project", ("/tmp/existing-project",))]


@pytest.mark.asyncio
async def test_engine_services_notify_tool_call_in_progress() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    await services.notify_tool_call_in_progress("tc_2")

    assert rec.calls == [("notify_tool_call_in_progress", ("tc_2",))]


@pytest.mark.asyncio
async def test_engine_services_add_security_rule_forwards_args() -> None:
    rec = _Recorder()
    services = _make_services(rec)

    await services.add_security_rule("session", "git", "push")

    assert rec.calls == [("add_security_rule", ("session", "git", "push"))]


# ---------------------------------------------------------------------------
# _events.EngineEmitters
# ---------------------------------------------------------------------------


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


class _FakeTransient:
    def __init__(self) -> None:
        self.markers: list[dict[str, object]] = []

    def append_marker(self, marker: dict[str, object]) -> None:
        self.markers.append(marker)


def _make_emitters(
    *, stats: dict[str, object] | None = None
) -> tuple[EngineEmitters, _FakeSink, _FakeTransient]:
    sink = _FakeSink()
    transient = _FakeTransient()
    session = SessionState(session_id="s1")
    emitters = EngineEmitters(sink, session, lambda: stats or {"current_tokens": 0}, transient)
    return emitters, sink, transient


def test_add_cost_accumulates() -> None:
    emitters, _sink, _transient = _make_emitters()
    assert emitters.cumulative_usd == 0.0

    emitters.add_cost(0.5)
    emitters.add_cost(0.25)

    assert emitters.cumulative_usd == 0.75


@pytest.mark.asyncio
async def test_handle_stream_event_thinking_delta() -> None:
    emitters, sink, _ = _make_emitters()

    await emitters.handle_stream_event(ThinkingDelta(text="hmm"), "stream_1")

    assert len(sink.sent) == 1
    assert sink.sent[0].kind == "thinking_chunk"
    assert sink.sent[0].correlation_id == "stream_1"


@pytest.mark.asyncio
async def test_handle_stream_event_token_delta() -> None:
    emitters, sink, _ = _make_emitters()

    await emitters.handle_stream_event(TokenDelta(text="hi"), "stream_1")

    assert sink.sent[0].kind == "stream_chunk"


@pytest.mark.asyncio
async def test_handle_stream_event_tool_call_arg_delta() -> None:
    emitters, sink, _ = _make_emitters()

    await emitters.handle_stream_event(
        ToolCallArgDelta(tool_name="run_command", text='{"comm'), "stream_1"
    )

    assert sink.sent[0].kind == "toolgen_chunk"
    assert sink.sent[0].payload["tool_name"] == "run_command"


@pytest.mark.asyncio
async def test_handle_stream_event_turn_end_is_ignored() -> None:
    emitters, sink, _ = _make_emitters()
    usage = Usage(
        input_tokens=1, output_tokens=1, cache_write_tokens=0, cache_read_tokens=0, model="m"
    )

    await emitters.handle_stream_event(TurnEnd(usage=usage, stop_reason="end_turn"), "stream_1")

    assert sink.sent == []


@pytest.mark.asyncio
async def test_emit_state_also_emits_context_stats() -> None:
    emitters, sink, _ = _make_emitters(stats={"current_tokens": 42})

    await emitters.emit_state()

    kinds = [env.payload["type"] for env in sink.sent]
    assert kinds == ["state", "context.stats"]
    assert sink.sent[1].payload["current_tokens"] == 42


@pytest.mark.asyncio
async def test_emit_context_compacting() -> None:
    emitters, sink, _ = _make_emitters()

    await emitters.emit_context_compacting(True)

    assert sink.sent[0].payload == {"type": "context.compacting", "active": True}


@pytest.mark.asyncio
async def test_emit_usage_reports_tokens_and_running_cost() -> None:
    emitters, sink, _ = _make_emitters()
    emitters.add_cost(1.23)
    usage = Usage(
        input_tokens=10, output_tokens=20, cache_write_tokens=1, cache_read_tokens=2, model="m"
    )
    turn_end = TurnEnd(usage=usage, stop_reason="end_turn")

    await emitters.emit_usage(turn_end, "claude-x", 2.5)

    payload = sink.sent[0].payload
    assert payload["type"] == "usage.update"
    assert payload["cumulative_usd"] == 1.23
    assert payload["duration_seconds"] == 2.5
    assert payload["last_call_tokens"] == {
        "input": 10,
        "output": 20,
        "cache_write": 1,
        "cache_read": 2,
    }
    assert payload["model"] == "claude-x"


@pytest.mark.asyncio
async def test_emit_cost_only_has_no_call_tokens() -> None:
    emitters, sink, _ = _make_emitters()
    emitters.add_cost(0.01)

    await emitters.emit_cost_only()

    payload = sink.sent[0].payload
    assert payload["last_call_tokens"] is None
    assert payload["cumulative_usd"] == 0.01
    assert payload["model"] == ""


@pytest.mark.asyncio
async def test_emit_session_naming() -> None:
    emitters, sink, _ = _make_emitters()
    await emitters.emit_session_naming(True)
    assert sink.sent[0].payload == {"type": "session.naming", "active": True}


@pytest.mark.asyncio
async def test_emit_web_search_note() -> None:
    emitters, sink, _ = _make_emitters()
    await emitters.emit_web_search_note("tc_1", "found something")
    assert sink.sent[0].payload == {
        "type": "web_search.note",
        "tool_call_id": "tc_1",
        "text": "found something",
    }


@pytest.mark.asyncio
async def test_notify_tool_call_in_progress() -> None:
    emitters, sink, _ = _make_emitters()
    await emitters.notify_tool_call_in_progress("tc_9")
    assert sink.sent[0].payload == {"type": "agent.tool_call_in_progress", "tool_call_id": "tc_9"}


@pytest.mark.asyncio
async def test_emit_error_persists_marker_and_sends_event() -> None:
    emitters, sink, transient = _make_emitters()

    await emitters.emit_error("boom", recoverable=True)

    assert transient.markers[0]["type"] == "error"
    assert transient.markers[0]["message"] == "boom"
    assert transient.markers[0]["recoverable"] is True
    assert "ts" in transient.markers[0]

    payload = sink.sent[0].payload
    assert payload["code"] == "runtime_error"
    assert payload["message"] == "boom"
    assert payload["recoverable"] is True


@pytest.mark.asyncio
async def test_emit_agent_started_includes_component() -> None:
    emitters, sink, _ = _make_emitters()
    emitters._session.component = "planning"

    await emitters.emit_agent_started("guide")

    assert sink.sent[0].payload == {
        "type": "agent.started",
        "agent": "guide",
        "component": "planning",
    }


@pytest.mark.asyncio
async def test_emit_agent_finished_includes_status_ok() -> None:
    emitters, sink, _ = _make_emitters()

    await emitters.emit_agent_finished("guide")

    assert sink.sent[0].payload == {
        "type": "agent.finished",
        "agent": "guide",
        "component": None,
        "status": "ok",
    }

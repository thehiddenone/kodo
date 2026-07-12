"""Tests for ``kodo.runtime._engine._subagents.SubagentMixin``.

``_run_author_critic_iteration`` already has focused coverage in
``test_engine_document_flow.py``; this file covers the rest of the
sub-agent dispatch surface: the spawn gate, the ungated
dependency-manager/web_search entry points, the subsession lifecycle
(open/drive/close/replay), and ``_render_task_input``/``_display_name``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kodo.llms import Message
from kodo.runtime import WorkflowEngine
from kodo.runtime._session import SessionState
from kodo.subagents import AgentLoadError
from kodo.toolspecs import SCHEMA_COMPLIANCE_KEY

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, *, allowed: dict[str, frozenset[str]] | None = None) -> None:
        self._allowed = allowed or {}

    def allowed_subagents(self, name: str) -> frozenset[str]:
        return self._allowed.get(name, frozenset())

    def get(self, name: str, autonomous: bool = False):
        if name == "unknown_agent":
            raise AgentLoadError(f"no such agent {name!r}")
        return SimpleNamespace(
            name=name,
            capability="medium",
            tools=frozenset(),
            system_prompt="sys",
            display_name="" if name != "architect" else "The Architect",
        )


class _FakeTransient:
    def __init__(self) -> None:
        self.subsession_messages: dict[str, list[tuple]] = {}
        self.markers: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.web_search_notes: list[tuple[str, list[str]]] = []
        self._rehydrate: dict[str, list[dict[str, object]]] = {}

    def append_subsession_message(self, subsession_id, role, content, kind=None) -> None:
        self.subsession_messages.setdefault(subsession_id, []).append((role, content, kind))

    def append_marker(self, marker: dict[str, object]) -> None:
        self.markers.append(marker)

    def update(self, **kwargs: object) -> None:
        self.updates.append(kwargs)

    def read_subsession_messages(self, subsession_id: str) -> list[dict[str, object]]:
        return self._rehydrate.get(subsession_id, [])

    def write_web_search_notes(self, tool_call_id: str, notes: list[str]) -> None:
        self.web_search_notes.append((tool_call_id, list(notes)))


class _FakeEmitters:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def emit_state(self) -> None:
        self.events.append(("state",))

    async def emit_agent_started(self, name: str) -> None:
        self.events.append(("started", name))

    async def emit_agent_finished(self, name: str) -> None:
        self.events.append(("finished", name))

    async def emit_web_search_note(self, tool_call_id: str, text: str) -> None:
        self.events.append(("note", tool_call_id, text))


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


class _FakeDispatcher:
    def __init__(self, *, returned_output: dict[str, object] | None = None) -> None:
        self.stop_requested = False
        self.returned_output = returned_output

    async def dispatch(self, name, args, call_id, recovered) -> str:
        return "unused"


def _make_engine(
    *,
    allowed: dict[str, frozenset[str]] | None = None,
    dispatcher_output: dict[str, object] | None = None,
) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._registry = _FakeRegistry(allowed=allowed)
    engine._replay_subsessions = None
    engine._transient = _FakeTransient()
    engine._session = SessionState(session_id="s1")
    engine._emitters = _FakeEmitters()
    engine._sink = _FakeSink()

    async def _resolve_plugin(capability: str, force_model_key: str | None = None):
        return (SimpleNamespace(name="fake-plugin"), "model-x", SimpleNamespace())

    engine._resolve_plugin = _resolve_plugin

    dispatcher = _FakeDispatcher(returned_output=dispatcher_output)
    engine._last_dispatcher = dispatcher
    engine._make_dispatcher = lambda agent_name, session_id, deadline=None: dispatcher

    engine.run_agent_turn_calls: list[dict[str, object]] = []

    async def _run_agent_turn(**kwargs):
        engine.run_agent_turn_calls.append(kwargs)
        return ([], [])

    engine._run_agent_turn = _run_agent_turn

    return engine


# ---------------------------------------------------------------------------
# _assert_can_spawn
# ---------------------------------------------------------------------------


def test_assert_can_spawn_allows_permitted_agent() -> None:
    engine = _make_engine(allowed={"guide": frozenset({"investigator"})})
    engine._assert_can_spawn("guide", "investigator")  # must not raise


def test_assert_can_spawn_rejects_unpermitted_agent() -> None:
    engine = _make_engine(allowed={"guide": frozenset({"investigator"})})
    with pytest.raises(PermissionError, match="not permitted to spawn"):
        engine._assert_can_spawn("guide", "planner")


def test_assert_can_spawn_rejects_direct_only_agent() -> None:
    engine = _make_engine(allowed={"guide": frozenset({"compactor"})})
    with pytest.raises(PermissionError, match="engine-driven only"):
        engine._assert_can_spawn("guide", "compactor")


def test_assert_can_spawn_checks_every_name() -> None:
    engine = _make_engine(allowed={"guide": frozenset({"architect"})})
    with pytest.raises(PermissionError):
        engine._assert_can_spawn("guide", "architect", "critic")


# ---------------------------------------------------------------------------
# _run_subagent (gated) / _run_dependency_manager / _run_web_search_agent (ungated)
# ---------------------------------------------------------------------------


async def test_run_subagent_denies_when_not_permitted() -> None:
    engine = _make_engine(allowed={"guide": frozenset()})
    with pytest.raises(PermissionError):
        await engine._run_subagent("guide", "investigator", {})


async def test_run_subagent_spawns_when_permitted() -> None:
    engine = _make_engine(
        allowed={"guide": frozenset({"investigator"})},
        dispatcher_output={"result": "ok"},
    )
    result = await engine._run_subagent("guide", "investigator", {"instructions": "go look"})
    assert result == {"result": "ok"}


async def test_run_dependency_manager_is_ungated() -> None:
    engine = _make_engine(dispatcher_output={"ok": True})
    result = await engine._run_dependency_manager({"action": "add"})
    assert result == {"ok": True}
    # No allow-list was consulted — the fixed depsmgr agent name was used.
    assert engine.run_agent_turn_calls[0]["agent_name"] == "toolchain_depsmgr"


async def test_run_web_search_agent_returns_themes_and_note() -> None:
    engine = _make_engine()

    async def _run_silent_tool_loop_turn(*args, on_round_text=None, **kwargs):
        if on_round_text is not None:
            await on_round_text("found something")
        return {"themes": ["a", "b"], "note": "summary note"}

    engine._run_silent_tool_loop_turn = _run_silent_tool_loop_turn

    result = await engine._run_web_search_agent({"query": "x", "timeout": 30}, "tc_1")

    assert result == {"themes": ["a", "b"], "note": "summary note"}
    assert engine._emitters.events == [("note", "tc_1", "found something")]
    assert engine._transient.web_search_notes == [("tc_1", ["found something"])]


async def test_run_web_search_agent_times_out_with_no_result() -> None:
    engine = _make_engine()

    async def _run_silent_tool_loop_turn(*args, on_round_text=None, **kwargs):
        return None

    engine._run_silent_tool_loop_turn = _run_silent_tool_loop_turn

    result = await engine._run_web_search_agent({"query": "x"}, "tc_1")

    assert result == {"themes": [], "note": "Search timed out before a report could be produced."}
    # No notes were produced, so nothing is written to the sidecar file.
    assert engine._transient.web_search_notes == []


async def test_run_web_search_agent_coerces_non_list_non_str_result_fields() -> None:
    engine = _make_engine()

    async def _run_silent_tool_loop_turn(*args, **kwargs):
        return {"themes": "not-a-list", "note": 42}

    engine._run_silent_tool_loop_turn = _run_silent_tool_loop_turn

    result = await engine._run_web_search_agent({"query": "x"}, "tc_1")

    assert result == {"themes": [], "note": ""}


async def test_run_web_search_agent_clamps_timeout_to_max() -> None:
    engine = _make_engine()
    captured: dict[str, object] = {}

    async def _run_silent_tool_loop_turn(
        routing, plugin, model_id, agent, messages, dispatcher, deadline, **kwargs
    ):
        captured["deadline"] = deadline
        return {"themes": [], "note": ""}

    import time

    before = time.time()
    engine._run_silent_tool_loop_turn = _run_silent_tool_loop_turn

    await engine._run_web_search_agent({"query": "x", "timeout": 99999}, "tc_1")

    # Clamped to _MAX_WEB_SEARCH_TIMEOUT_S (600s), not the requested 99999s.
    assert captured["deadline"] < before + 601


# ---------------------------------------------------------------------------
# _render_task_input
# ---------------------------------------------------------------------------


def test_render_task_input_empty_dict() -> None:
    assert WorkflowEngine._render_task_input({}) == "(no task)"


def test_render_task_input_instructions_only() -> None:
    text = WorkflowEngine._render_task_input({"instructions": "  Do the thing.  "})
    assert text == "# Task\n\nDo the thing."


def test_render_task_input_with_other_fields() -> None:
    text = WorkflowEngine._render_task_input(
        {"instructions": "Do it", "paths": ["a.md", "b.md"], "count": 3, "empty_list": []}
    )
    assert "# Task\n\nDo it" in text
    assert "## Inputs" in text
    assert "- paths: a.md, b.md" in text
    assert "- count: 3" in text
    assert "- empty_list: (none)" in text


def test_render_task_input_no_instructions_only_other_fields() -> None:
    text = WorkflowEngine._render_task_input({"target": "file.md"})
    assert text == "## Inputs\n- target: file.md"


def test_render_task_input_blank_instructions_treated_as_absent() -> None:
    text = WorkflowEngine._render_task_input({"instructions": "   "})
    assert text == "(no task)"


# ---------------------------------------------------------------------------
# _display_name
# ---------------------------------------------------------------------------


def test_display_name_uses_frontmatter_when_set() -> None:
    engine = _make_engine()
    assert engine._display_name("architect") == "The Architect"


def test_display_name_falls_back_to_name_when_blank() -> None:
    engine = _make_engine()
    assert engine._display_name("investigator") == "investigator"


def test_display_name_falls_back_on_load_error() -> None:
    engine = _make_engine()
    assert engine._display_name("unknown_agent") == "unknown_agent"


# ---------------------------------------------------------------------------
# _open_subsession / _close_subsession
# ---------------------------------------------------------------------------


async def test_open_subsession_records_marker_active_pointer_and_event() -> None:
    engine = _make_engine()
    engine._session.agent = "guide"

    await engine._open_subsession("investigator", "sub1", "look into it")

    assert engine._transient.markers[-1]["type"] == "subsession_start"
    assert engine._transient.markers[-1]["subsession_id"] == "sub1"
    assert engine._transient.markers[-1]["parent_display_name"] == "guide"
    assert engine._transient.updates[-1]["active_subsession"]["subsession_id"] == "sub1"
    assert engine._sink.sent[-1].payload["type"] == "subsession.started"
    assert engine._sink.sent[-1].payload["task"] == "look into it"


async def test_open_subsession_defaults_parent_to_guide_when_no_active_agent() -> None:
    engine = _make_engine()
    engine._session.agent = None

    await engine._open_subsession("investigator", "sub1")

    assert engine._transient.markers[-1]["parent_display_name"] == "guide"


async def test_close_subsession_marks_failed_when_schema_noncompliant() -> None:
    engine = _make_engine()
    output = {SCHEMA_COMPLIANCE_KEY: False}

    await engine._close_subsession("investigator", "sub1", output)

    assert engine._transient.markers[-1]["failed"] is True
    assert engine._transient.markers[-1]["result"] == output
    assert engine._transient.updates[-1] == {"active_subsession": None}
    assert engine._sink.sent[-1].payload["failed"] is True


async def test_close_subsession_not_failed_when_schema_compliant() -> None:
    engine = _make_engine()
    await engine._close_subsession("investigator", "sub1", {SCHEMA_COMPLIANCE_KEY: True})
    assert engine._transient.markers[-1]["failed"] is False


# ---------------------------------------------------------------------------
# _drive_subsession
# ---------------------------------------------------------------------------


async def test_drive_subsession_returns_dispatcher_output() -> None:
    engine = _make_engine(dispatcher_output={"summary": "done"})
    seed = [Message(role="user", content="go")]

    output = await engine._drive_subsession("investigator", "sub1", seed)

    assert output == {"summary": "done"}
    assert engine._session.agent == "investigator"
    assert ("started", "investigator") in engine._emitters.events
    assert ("finished", "investigator") in engine._emitters.events
    assert engine._sink.sent[-1].kind == "stream_end"


async def test_drive_subsession_synthesizes_fallback_when_no_return_result() -> None:
    engine = _make_engine(dispatcher_output=None)
    output = await engine._drive_subsession("investigator", "sub1", [])
    assert output == {SCHEMA_COMPLIANCE_KEY: False}


async def test_drive_subsession_persist_callback_appends_subsession_messages() -> None:
    engine = _make_engine(dispatcher_output={"ok": True})

    async def _run_agent_turn(**kwargs):
        kwargs["persist"]([Message(role="assistant", content="partial")])
        return ([], [])

    engine._run_agent_turn = _run_agent_turn

    await engine._drive_subsession("investigator", "sub1", [])

    assert engine._transient.subsession_messages["sub1"] == [("assistant", "partial", None)]


# ---------------------------------------------------------------------------
# _spawn_subagent
# ---------------------------------------------------------------------------


async def test_spawn_subagent_rejects_direct_only_agent() -> None:
    engine = _make_engine()
    result = await engine._spawn_subagent("compactor", {})
    assert result == {}
    assert engine._transient.markers == []  # never opened a subsession


async def test_spawn_subagent_fresh_run_opens_and_closes_subsession() -> None:
    engine = _make_engine(dispatcher_output={"primary_path": "a.md"})

    result = await engine._spawn_subagent("investigator", {"instructions": "look"})

    assert result == {"primary_path": "a.md"}
    kinds = [m["type"] for m in engine._transient.markers]
    assert kinds == ["subsession_start", "subsession_end"]
    # The seed message was persisted as a subagent_task, not a plain message.
    sub_id = engine._transient.markers[0]["subsession_id"]
    assert engine._transient.subsession_messages[sub_id][0][2] == "subagent_task"


async def test_spawn_subagent_replay_mode_consumes_ledger_instead_of_running() -> None:
    engine = _make_engine()
    engine._replay_subsessions = [
        {"subsession_id": "sub1", "agent": "investigator", "completed": True, "result": {"x": 1}}
    ]

    result = await engine._spawn_subagent("investigator", {"instructions": "look"})

    assert result == {"x": 1}
    # A completed replay never opens a new subsession.
    assert engine._transient.markers == []


async def test_spawn_subagent_clears_replay_flag_when_ledger_empty() -> None:
    engine = _make_engine(dispatcher_output={"ok": True})
    engine._replay_subsessions = []  # falsy but not None

    await engine._spawn_subagent("investigator", {})

    assert engine._replay_subsessions is None


# ---------------------------------------------------------------------------
# _replay_next_subsession
# ---------------------------------------------------------------------------


async def test_replay_next_subsession_completed_returns_stored_dict_result() -> None:
    engine = _make_engine()
    engine._replay_subsessions = [
        {"subsession_id": "sub1", "agent": "investigator", "completed": True, "result": {"x": 1}}
    ]

    result = await engine._replay_next_subsession("investigator")

    assert result == {"x": 1}
    assert engine._replay_subsessions is None


async def test_replay_next_subsession_completed_non_dict_result_returns_empty() -> None:
    engine = _make_engine()
    engine._replay_subsessions = [
        {
            "subsession_id": "sub1",
            "agent": "investigator",
            "completed": True,
            "result": ["not", "a", "dict"],
        }
    ]

    result = await engine._replay_next_subsession("investigator")

    assert result == {}


async def test_replay_next_subsession_active_rehydrates_and_drives(monkeypatch) -> None:
    engine = _make_engine(dispatcher_output={"resumed": True})
    engine._replay_subsessions = [
        {"subsession_id": "sub1", "agent": "investigator", "completed": False, "result": {}},
        {"subsession_id": "sub2", "agent": "planner", "completed": False, "result": {}},
    ]
    engine._transient._rehydrate["sub1"] = [{"role": "user", "content": "seed"}]

    result = await engine._replay_next_subsession("investigator")

    assert result == {"resumed": True}
    # Only the consumed entry was popped; the ledger is not yet exhausted.
    assert engine._replay_subsessions == [
        {"subsession_id": "sub2", "agent": "planner", "completed": False, "result": {}}
    ]
    # Driving to completion also closes the subsession.
    assert engine._transient.markers[-1]["type"] == "subsession_end"


async def test_replay_next_subsession_pops_last_entry_clears_ledger() -> None:
    engine = _make_engine(dispatcher_output={"ok": True})
    engine._replay_subsessions = [
        {"subsession_id": "sub1", "agent": "investigator", "completed": False, "result": {}}
    ]
    engine._transient._rehydrate["sub1"] = []

    await engine._replay_next_subsession("investigator")

    assert engine._replay_subsessions is None

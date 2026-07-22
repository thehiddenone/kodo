"""Tests for ``kodo.runtime._engine._core.WorkflowEngine`` — construction,
session lifecycle, client-facing handlers, and the environment helpers.

Builds a real ``WorkflowEngine`` via its actual ``__init__`` (real
``TransientStore``/``WorkspaceLayout``/``SessionWorkspace``/collaborators,
all rooted under a temp dir so nothing touches the real ``~/.kodo``) with
only the true external boundaries faked: the client sink, the approval
gate, the API key provider, the subagent registry, and the LLM gateway.
This exercises the actual wiring in ``__init__`` rather than re-deriving it
with stubs, catching constructor-level regressions the mixin-level tests
(``test_engine_*.py``) cannot see.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodo.project import SessionWorkspace, WorkspaceLayout
from kodo.runtime import WorkflowEngine
from kodo.runtime._checkpoints import CheckpointState
from kodo.runtime._engine import _core
from kodo.runtime._gates import ApprovalResponse
from kodo.state import TransientStore
from kodo.subagents import AgentLoadError


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


class _FakeGate:
    def __init__(self, *, action: str = "agree", feedback: str = "") -> None:
        self.action = action
        self.feedback = feedback
        self.calls: list[tuple[str, str | None, str]] = []

    async def fire_approval(
        self, gate_type: str, *, artifact_id=None, summary: str = ""
    ) -> ApprovalResponse:
        self.calls.append((gate_type, artifact_id, summary))
        return ApprovalResponse(action=self.action, feedback=self.feedback)


class _FakeKeyProvider:
    async def get_key(self, vendor: str):
        raise AssertionError("not exercised by these tests")


class _FakeRegistry:
    def __init__(self, *, known: dict[str, object] | None = None) -> None:
        self._known = known or {}

    def get(self, name: str, autonomous: bool = False):
        if name not in self._known:
            raise AgentLoadError(f"unknown agent {name!r}")
        return self._known[name]

    def spec_for(self, name: str):
        return None


def _make_engine(
    tmp_path: Path,
    *,
    gate: _FakeGate | None = None,
    registry: _FakeRegistry | None = None,
    physical_root: Path | None = None,
    settings: dict[str, object] | None = None,
) -> tuple[WorkflowEngine, TransientStore, _FakeSink, _FakeGate]:
    kodo_dir = tmp_path / "home"
    workspace_layout = WorkspaceLayout(root=kodo_dir)
    transient = TransientStore(kodo_dir)
    session_workspace = SessionWorkspace(physical_root=physical_root or tmp_path)
    sink = _FakeSink()
    gate = gate or _FakeGate()
    engine = WorkflowEngine(
        sink=sink,
        gate=gate,
        key_provider=_FakeKeyProvider(),
        get_settings=lambda: settings or {},
        transient=transient,
        workspace_layout=workspace_layout,
        registry=registry or _FakeRegistry(),
        gateway=SimpleNamespace(),
        session_workspace=session_workspace,
    )
    return engine, transient, sink, gate


async def _cancel_worker(engine: WorkflowEngine) -> None:
    if engine._worker is not None:
        engine._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await engine._worker


# ---------------------------------------------------------------------------
# __init__ / properties
# ---------------------------------------------------------------------------


def test_init_wires_collaborators_and_defaults(tmp_path: Path) -> None:
    engine, _transient, _sink, gate = _make_engine(tmp_path)

    assert engine.session is engine._session
    assert engine.gate is gate
    assert engine.session_id == ""
    assert engine.current_project is None
    assert engine._layout is None
    assert engine._worker is None
    assert engine._main_messages == []


def test_session_name_reads_from_transient(tmp_path: Path) -> None:
    engine, transient, _sink, _gate = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)
    assert engine.session_name == transient.session_name


# ---------------------------------------------------------------------------
# _require_layout / _agent_available
# ---------------------------------------------------------------------------


def test_require_layout_raises_when_unbound(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    with pytest.raises(RuntimeError, match="No current project is bound"):
        engine._require_layout()


def test_agent_available_true_for_known_agent(tmp_path: Path) -> None:
    registry = _FakeRegistry(known={"guide": SimpleNamespace(capability="medium")})
    engine, _t, _s, _g = _make_engine(tmp_path, registry=registry)
    assert engine._agent_available("guide") is True


def test_agent_available_false_for_unknown_agent(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    assert engine._agent_available("nonexistent") is False


# ---------------------------------------------------------------------------
# start() — fresh session
# ---------------------------------------------------------------------------


async def test_start_fresh_session_spawns_worker(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    try:
        await engine.start("session-1", resumed=False)
        assert engine.session_id == "session-1"
        assert engine._worker is not None
        assert not engine._worker.done()
        assert engine._main_messages == []
    finally:
        await _cancel_worker(engine)


async def test_start_resumed_session_restores_prefs_and_messages(tmp_path: Path) -> None:
    # Seed a prior session on disk, then attach a fresh engine as "resumed".
    kodo_dir = tmp_path / "home"
    seed_transient = TransientStore(kodo_dir)
    seed_transient.attach_session("session-2", resumed=False)
    seed_transient.append_message("user", "hello from before", entry_agent="guide")
    seed_transient.update(
        autonomous=True,
        workflow_mode="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    )

    engine, _transient, _s, _g = _make_engine(tmp_path)
    try:
        await engine.start("session-2", resumed=True)

        assert len(engine._main_messages) == 1
        assert engine._session.autonomous is True
        assert engine._session.workflow_mode == "problem_solving"
        assert engine._session.edit_control == "allow_all"
        assert engine._session.command_control == "permissive"
        assert engine._compactor.context_tokens > 0
    finally:
        await _cancel_worker(engine)


async def test_start_resumed_session_rebinds_persisted_project(tmp_path: Path) -> None:
    from kodo.project import ProjectLayout

    project_root = tmp_path / "proj"
    ProjectLayout(project_root).init()

    kodo_dir = tmp_path / "home"
    seed_transient = TransientStore(kodo_dir)
    seed_transient.attach_session("session-3", resumed=False)
    seed_transient.update(current_project={"root": str(project_root), "name": "proj"})

    engine, _transient, _s, _g = _make_engine(tmp_path)
    try:
        await engine.start("session-3", resumed=True)
        assert engine.current_project is not None
        assert engine.current_project["name"] == "proj"
        assert engine._layout is not None
    finally:
        await _cancel_worker(engine)


async def test_start_resumed_with_pending_prompt_schedules_resume_task(tmp_path: Path) -> None:
    kodo_dir = tmp_path / "home"
    seed_transient = TransientStore(kodo_dir)
    seed_transient.attach_session("session-5", resumed=False)
    seed_transient.update(
        pending_prompt={"kind": "approval", "gate_type": "document_review", "summary": "Review x"}
    )

    gate = _FakeGate(action="agree")
    engine, _transient, _s, _g = _make_engine(tmp_path, gate=gate)
    try:
        await engine.start("session-5", resumed=True)
        assert engine._resume_subsession_pending is False
        # Give the fire-and-forget resume task a turn to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert gate.calls == [("document_review", None, "Review x")]
    finally:
        await _cancel_worker(engine)


async def test_start_resumed_with_dangling_tool_use_sets_resume_pending(tmp_path: Path) -> None:

    kodo_dir = tmp_path / "home"
    seed_transient = TransientStore(kodo_dir)
    seed_transient.attach_session("session-4", resumed=False)
    seed_transient.append_message(
        "assistant",
        [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}}],
        entry_agent="guide",
    )

    engine, _transient, _s, _g = _make_engine(tmp_path)
    try:
        await engine.start("session-4", resumed=True)
        assert engine._resume_subsession_pending is True
    finally:
        await _cancel_worker(engine)


# ---------------------------------------------------------------------------
# handle_workspace_folders
# ---------------------------------------------------------------------------


async def test_handle_workspace_folders_updates_session_workspace(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    folder = tmp_path / "myproj"
    folder.mkdir()

    await engine.handle_workspace_folders(str(tmp_path), {"myproj": str(folder)})

    assert engine._session_workspace.physical_root == tmp_path.resolve()
    assert "myproj" in engine._session_workspace.folders


async def test_handle_workspace_folders_blank_physical_root_leaves_it_unchanged(
    tmp_path: Path,
) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)
    await engine.handle_workspace_folders("", {})
    assert engine._session_workspace.physical_root == tmp_path.resolve()


async def test_handle_workspace_folders_persists_workspace_shape(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    folder = tmp_path / "myproj"
    folder.mkdir()

    await engine.handle_workspace_folders(
        str(tmp_path), {"myproj": str(folder)}, "/home/dev/dev.code-workspace"
    )

    assert transient.workspace_physical_root == str(tmp_path)
    assert transient.workspace_folders == {"myproj": str(folder)}
    assert transient.workspace_code_file == "/home/dev/dev.code-workspace"
    assert (
        engine._session_workspace.code_workspace_file
        == Path("/home/dev/dev.code-workspace").resolve()
    )


async def test_handle_workspace_folders_defaults_code_file_to_none(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    await engine.handle_workspace_folders(str(tmp_path), {})
    assert transient.workspace_code_file is None
    assert engine._session_workspace.code_workspace_file is None


async def test_handle_workspace_folders_new_folder_is_always_accepted(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    await engine.handle_workspace_folders(str(tmp_path), {"a": str(a)})
    await engine.handle_workspace_folders(str(tmp_path), {"a": str(a), "b": str(b)})

    assert transient.workspace_folders == {"a": str(a), "b": str(b)}


async def test_handle_workspace_folders_drops_unlocked_folder_on_removal(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    await engine.handle_workspace_folders(str(tmp_path), {"a": str(a), "b": str(b)})
    # Neither folder was ever checkpointed — removing one from the live push
    # drops it, exactly as pushed.
    await engine.handle_workspace_folders(str(tmp_path), {"a": str(a)})

    assert transient.workspace_folders == {"a": str(a)}


async def test_handle_workspace_folders_keeps_locked_folder_after_removal(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    await engine.handle_workspace_folders(str(tmp_path), {"a": str(a), "b": str(b)})
    transient.lock_workspace_path(str(b.resolve()))

    # The user removes "b" from the live workspace — it must survive in the
    # remembered folder map because it's locked.
    await engine.handle_workspace_folders(str(tmp_path), {"a": str(a)})

    assert transient.workspace_folders == {"a": str(a), "b": str(b)}


async def test_handle_workspace_folders_locked_folder_name_collision_is_disambiguated(
    tmp_path: Path,
) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    old_b = tmp_path / "old_b"
    new_b = tmp_path / "new_b"
    old_b.mkdir()
    new_b.mkdir()

    await engine.handle_workspace_folders(str(tmp_path), {"b": str(old_b)})
    transient.lock_workspace_path(str(old_b.resolve()))

    # A different folder now claims the name "b" the locked folder used to
    # have — the locked folder must still survive, under some other key.
    await engine.handle_workspace_folders(str(tmp_path), {"b": str(new_b)})

    assert transient.workspace_folders["b"] == str(new_b)
    assert str(old_b) in transient.workspace_folders.values()


# ---------------------------------------------------------------------------
# bind_project / _bind_project
# ---------------------------------------------------------------------------


def _make_project(root: Path) -> None:
    from kodo.project import ProjectLayout

    ProjectLayout(root).init()


async def test_bind_project_success_emits_event_and_persists(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.bind_project(str(project_root), "proj")

    assert engine.current_project == {"root": str(project_root.resolve()), "name": "proj"}
    assert engine._layout is not None
    bound_events = [e for e in sink.sent if e.payload.get("type") == "project.bound"]
    assert len(bound_events) == 1
    assert transient.current_project == {"root": str(project_root.resolve()), "name": "proj"}


async def test_bind_project_invalid_layout_emits_error(tmp_path: Path) -> None:
    bad_root = tmp_path / "not-a-project"
    bad_root.mkdir()
    engine, _transient, sink, _g = _make_engine(tmp_path)

    await engine.bind_project(str(bad_root), "bad")

    assert engine.current_project is None
    assert engine._layout is None
    error_events = [e for e in sink.sent if e.payload.get("type") == "error"]
    assert len(error_events) == 1


async def test_bind_project_idempotent_same_root_is_noop(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, _t, sink, _g = _make_engine(tmp_path)

    await engine.bind_project(str(project_root), "proj")
    sink.sent.clear()
    await engine.bind_project(str(project_root), "proj")

    assert sink.sent == []  # second bind is a silent no-op


async def test_bind_project_different_root_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    other_root = tmp_path / "other"
    _make_project(project_root)
    _make_project(other_root)
    engine, _t, sink, _g = _make_engine(tmp_path)

    await engine.bind_project(str(project_root), "proj")
    sink.sent.clear()
    await engine.bind_project(str(other_root), "other")

    assert engine.current_project["root"] == str(project_root.resolve())
    error_events = [e for e in sink.sent if e.payload.get("type") == "error"]
    assert len(error_events) == 1
    assert "fixed for this session" in error_events[0].payload["message"]


async def test_bind_project_resume_skips_emit(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, transient, sink, _g = _make_engine(tmp_path)

    await engine._bind_project(str(project_root), "proj", emit=False)

    assert engine.current_project is not None
    assert sink.sent == []


# ---------------------------------------------------------------------------
# _resume_pending_prompt
# ---------------------------------------------------------------------------


async def test_resume_pending_prompt_approval_agree_requeues_text(tmp_path: Path) -> None:
    gate = _FakeGate(action="agree")
    engine, _t, _s, _g = _make_engine(tmp_path, gate=gate)

    await engine._resume_pending_prompt(
        {
            "kind": "approval",
            "gate_type": "document_review",
            "artifact_id": "a.md",
            "summary": "Review a.md",
        }
    )

    assert gate.calls == [("document_review", "a.md", "Review a.md")]
    queued = engine._queue.get_nowait()
    assert "Review a.md" in queued["text"]
    assert "agree" in queued["text"]


async def test_resume_pending_prompt_approval_feedback_includes_feedback_text(
    tmp_path: Path,
) -> None:
    gate = _FakeGate(action="feedback", feedback="needs more detail")
    engine, _t, _s, _g = _make_engine(tmp_path, gate=gate)

    await engine._resume_pending_prompt(
        {"kind": "approval", "gate_type": "document_review", "summary": "Review x"}
    )

    queued = engine._queue.get_nowait()
    assert "needs more detail" in queued["text"]


async def test_resume_pending_prompt_unknown_kind_clears_pending(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine._resume_pending_prompt({"kind": "legacy_question"})

    assert engine._queue.empty()
    assert transient.pending_prompt is None


async def test_resume_pending_prompt_gate_failure_is_swallowed(tmp_path: Path) -> None:
    class _BoomGate(_FakeGate):
        async def fire_approval(self, *a, **k):
            raise RuntimeError("client disconnected")

    engine, _t, _s, _g = _make_engine(tmp_path, gate=_BoomGate())

    await engine._resume_pending_prompt({"kind": "approval", "summary": "x"})

    assert engine._queue.empty()


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


async def test_stop_when_not_running_just_resets_phase(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    try:
        await engine.start("s1", resumed=False)
        await engine.stop()
        assert engine._session.phase == "stopped"
        assert engine._session.agent is None
        assert engine._worker is not None
    finally:
        await _cancel_worker(engine)


async def test_stop_while_running_persists_interrupted_turn(tmp_path: Path) -> None:
    from kodo.llms import Message

    engine, transient, _s, _g = _make_engine(tmp_path)
    try:
        await engine.start("s1", resumed=False)
        engine._session.phase = "running"
        engine._session.agent = "guide"
        engine._main_messages = [Message(role="user", content="go")]

        await engine.stop()

        assert engine._session.phase == "stopped"
        assert any("interrupted" in str(m.content) for m in engine._main_messages)
    finally:
        await _cancel_worker(engine)


# ---------------------------------------------------------------------------
# handle_prompt_submit / mode + control setters
# ---------------------------------------------------------------------------


async def test_handle_prompt_submit_parses_attachments_and_enqueues(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.handle_prompt_submit("hello world", "req-1")

    queued = engine._queue.get_nowait()
    assert queued == {"text": "hello world", "attachments": [], "request_id": "req-1"}
    assert transient.last_prompt == "hello world"


async def test_handle_mode_set_updates_session_and_persists(tmp_path: Path) -> None:
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.handle_mode_set(True)

    assert engine._session.autonomous is True
    assert transient.autonomous is True
    assert any(e.payload.get("type") == "state" for e in sink.sent)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("problem_solving", "problem_solving"),
        ("judge", "judge"),
        ("guided", "guided"),
        ("bogus", "guided"),
    ],
)
async def test_handle_workflow_set_normalizes_unknown_modes(
    tmp_path: Path, mode: str, expected: str
) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.handle_workflow_set(mode)

    assert engine._session.workflow_mode == expected
    assert transient.workflow_mode == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("review_all", "review_all"),
        ("allow_all", "allow_all"),
        ("smart", "smart"),
        ("garbage", "smart"),
    ],
)
async def test_handle_edit_control_set_normalizes(
    tmp_path: Path, value: str, expected: str
) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.handle_edit_control_set(value)

    assert engine._session.edit_control == expected
    assert transient.edit_control == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("defensive", "defensive"),
        ("permissive", "permissive"),
        ("smart", "smart"),
        ("garbage", "smart"),
    ],
)
async def test_handle_command_control_set_normalizes(
    tmp_path: Path, value: str, expected: str
) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.handle_command_control_set(value)

    assert engine._session.command_control == expected
    assert transient.command_control == expected


# ---------------------------------------------------------------------------
# add_security_rule (doc/SECURITY_RULES_PLAN.md §2.4)
# ---------------------------------------------------------------------------


async def test_add_security_rule_session_scope_updates_session_and_transient(
    tmp_path: Path,
) -> None:
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.add_security_rule("session", "git", "push")

    assert engine._session.security_rules == frozenset({("git", "push")})
    assert transient.security_rules == frozenset({("git", "push")})
    assert sink.sent[-1].payload == {
        "type": "security.rule_added",
        "scope": "session",
        "executable": "git",
        "subcommand": "push",
    }


async def test_add_security_rule_session_scope_survives_resume(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)
    await engine.add_security_rule("session", "npm", "publish")

    # A fresh engine resuming the same session id should see the same rule.
    engine2, transient2, _s2, _g2 = _make_engine(tmp_path)
    transient2.attach_session("s1", resumed=True)
    engine2._session.security_rules = transient2.security_rules

    assert engine2._session.security_rules == frozenset({("npm", "publish")})


async def test_add_security_rule_global_scope_does_not_touch_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.add_security_rule("global", "docker", "run")

    assert engine._session.security_rules == frozenset()
    assert transient.security_rules == frozenset()
    from kodo.security import global_rules

    assert ("docker", "run") in global_rules()


async def test_add_security_rule_unknown_scope_is_a_noop(tmp_path: Path) -> None:
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.add_security_rule("bogus", "git", "push")

    assert engine._session.security_rules == frozenset()
    assert transient.security_rules == frozenset()
    assert sink.sent == []


# ---------------------------------------------------------------------------
# add_security_path_rule (doc/SECURITY_RULES_PLAN.md §2.7)
# ---------------------------------------------------------------------------


async def test_add_security_path_rule_session_scope_updates_session_and_transient(
    tmp_path: Path,
) -> None:
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.add_security_path_rule("session", "cat", "/etc/hosts")

    assert engine._session.security_path_rules == frozenset({("cat", "/etc/hosts")})
    assert transient.security_path_rules == frozenset({("cat", "/etc/hosts")})
    assert sink.sent[-1].payload == {
        "type": "security.rule_added",
        "scope": "session",
        "executable": "cat",
        "subcommand": "/etc/hosts",
    }


async def test_add_security_path_rule_session_scope_survives_resume(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)
    await engine.add_security_path_rule("session", "cd", "/outside/path")

    # A fresh engine resuming the same session id should see the same rule.
    engine2, transient2, _s2, _g2 = _make_engine(tmp_path)
    transient2.attach_session("s1", resumed=True)
    engine2._session.security_path_rules = transient2.security_path_rules

    assert engine2._session.security_path_rules == frozenset({("cd", "/outside/path")})


async def test_add_security_path_rule_global_scope_does_not_touch_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    engine, transient, _s, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.add_security_path_rule("global", "cat", "/etc/hosts")

    assert engine._session.security_path_rules == frozenset()
    assert transient.security_path_rules == frozenset()
    from kodo.security import global_path_rules

    assert ("cat", "/etc/hosts") in global_path_rules()


async def test_add_security_path_rule_unknown_scope_is_a_noop(tmp_path: Path) -> None:
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)

    await engine.add_security_path_rule("bogus", "cat", "/etc/hosts")

    assert engine._session.security_path_rules == frozenset()
    assert transient.security_path_rules == frozenset()
    assert sink.sent == []


# ---------------------------------------------------------------------------
# thinking_level: _current_base_llm / handle_thinking_level_set / start() seeding
# ---------------------------------------------------------------------------

# Real hardcoded registry entries (kodo.llms._local_registry) covering both
# thinking families plus a non-thinking model, so _current_base_llm() resolves
# a genuine base_llm without mocking the registry.
_QWEN_MODEL = "unsloth-qwen36-27b-q4-k-xl"  # base_llm=Qwen36-27B, 6 tiers
_GPT_OSS_MODEL = "unsloth-gpt-oss-20b-q4-k-xl"  # base_llm=GPT-OSS-20B, 3 tiers
_NO_THINKING_MODEL = "unsloth-qwen3-coder-next-80b-q4-k-xl"  # base_llm has no family


def test_current_base_llm_resolves_local_model(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _QWEN_MODEL}}
    )
    assert engine._current_base_llm() == "Qwen36-27B"


def test_current_base_llm_empty_for_cloud_mode(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(
        tmp_path, settings={"mode": "cloud", "active_cloud_vendor": "anthropic"}
    )
    assert engine._current_base_llm() == ""


def test_current_base_llm_empty_for_non_thinking_local_model(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _NO_THINKING_MODEL}}
    )
    assert engine._current_base_llm() == "Qwen3-Coder-Next-80B"


async def test_start_fresh_session_seeds_thinking_level_from_family_default(
    tmp_path: Path,
) -> None:
    engine, transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _GPT_OSS_MODEL}}
    )
    try:
        await engine.start("session-1", resumed=False)
        assert engine._session.thinking_level == "medium"
        assert transient.thinking_level == "medium"
    finally:
        await _cancel_worker(engine)


async def test_start_fresh_session_seeds_thinking_level_from_explicit_seed(
    tmp_path: Path,
) -> None:
    engine, _transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _QWEN_MODEL}}
    )
    try:
        await engine.start("session-1", resumed=False, thinking_level="minimal")
        assert engine._session.thinking_level == "minimal"
    finally:
        await _cancel_worker(engine)


async def test_start_fresh_session_thinking_level_empty_for_non_thinking_model(
    tmp_path: Path,
) -> None:
    engine, _transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _NO_THINKING_MODEL}}
    )
    try:
        await engine.start("session-1", resumed=False)
        assert engine._session.thinking_level == ""
    finally:
        await _cancel_worker(engine)


async def test_start_resumed_session_restores_valid_persisted_thinking_level(
    tmp_path: Path,
) -> None:
    kodo_dir = tmp_path / "home"
    seed_transient = TransientStore(kodo_dir)
    seed_transient.attach_session("session-2", resumed=False)
    seed_transient.update(thinking_level="huge")

    engine, _transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _QWEN_MODEL}}
    )
    try:
        await engine.start("session-2", resumed=True)
        assert engine._session.thinking_level == "huge"
    finally:
        await _cancel_worker(engine)


async def test_start_resumed_session_re_derives_thinking_level_if_model_changed(
    tmp_path: Path,
) -> None:
    # Persisted tier "huge" is invalid for GPT-OSS's 3-tier scale — simulates
    # the active model having changed to a different family while the session
    # was closed. Resume must self-heal to the new family's default rather
    # than carrying over a meaningless value.
    kodo_dir = tmp_path / "home"
    seed_transient = TransientStore(kodo_dir)
    seed_transient.attach_session("session-3", resumed=False)
    seed_transient.update(thinking_level="huge")

    engine, _transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _GPT_OSS_MODEL}}
    )
    try:
        await engine.start("session-3", resumed=True)
        assert engine._session.thinking_level == "medium"
    finally:
        await _cancel_worker(engine)


async def test_handle_thinking_level_set_accepts_valid_tier(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _QWEN_MODEL}}
    )
    transient.attach_session("s1", resumed=False)

    ok = await engine.handle_thinking_level_set("high")

    assert ok is True
    assert engine._session.thinking_level == "high"
    assert transient.thinking_level == "high"


async def test_handle_thinking_level_set_rejects_invalid_tier(tmp_path: Path) -> None:
    engine, transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _GPT_OSS_MODEL}}
    )
    transient.attach_session("s1", resumed=False)
    engine._session.thinking_level = "medium"

    # "unlimited" is a Qwen-family tier, not valid for GPT-OSS's 3-tier scale.
    ok = await engine.handle_thinking_level_set("unlimited")

    assert ok is False
    assert engine._session.thinking_level == "medium"


async def test_handle_thinking_level_set_rejects_nonempty_for_non_thinking_model(
    tmp_path: Path,
) -> None:
    engine, _transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _NO_THINKING_MODEL}}
    )
    ok = await engine.handle_thinking_level_set("medium")
    assert ok is False


async def test_handle_thinking_level_set_accepts_empty_for_non_thinking_model(
    tmp_path: Path,
) -> None:
    engine, transient, _s, _g = _make_engine(
        tmp_path, settings={"mode": "local", "models": {"local": _NO_THINKING_MODEL}}
    )
    transient.attach_session("s1", resumed=False)
    ok = await engine.handle_thinking_level_set("")
    assert ok is True
    assert engine._session.thinking_level == ""


def test_freeze_effective_modes_snapshots_both_toggles(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.autonomous = True
    engine._session.workflow_mode = "problem_solving"

    engine._freeze_effective_modes()

    assert engine._session.effective_autonomous is True
    assert engine._session.effective_workflow_mode == "problem_solving"


# ---------------------------------------------------------------------------
# handle_compact_now / handle_config_changed
# ---------------------------------------------------------------------------


async def test_handle_compact_now_enqueues_compact_task(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    await engine.handle_compact_now()
    assert engine._queue.get_nowait() == {"kind": "compact"}


async def test_handle_config_changed_enqueues_config_changed_task(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    await engine.handle_config_changed()
    assert engine._queue.get_nowait() == {"kind": "config_changed"}


# ---------------------------------------------------------------------------
# Checkpoint handlers (forwarded to the coordinator)
# ---------------------------------------------------------------------------


async def test_checkpoint_handlers_forward_to_coordinator(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    calls: list[tuple[str, tuple]] = []

    class _FakeCoordinator:
        async def undo(self, root, sha, resolution=None):
            calls.append(("undo", (root, sha, resolution)))
            return CheckpointState()

        async def redo(self, root, sha, resolution=None):
            calls.append(("redo", (root, sha, resolution)))
            return CheckpointState()

        async def rollback(self, root, sha, resolution=None):
            calls.append(("rollback", (root, sha, resolution)))
            return CheckpointState()

        async def roll_forward(self, root, sha, resolution=None):
            calls.append(("roll_forward", (root, sha, resolution)))
            return CheckpointState()

        async def state_for(self, root):
            calls.append(("state_for", (root,)))
            return CheckpointState()

    engine._checkpoints = _FakeCoordinator()

    await engine.handle_checkpoint_undo("root1", "sha1")
    await engine.handle_checkpoint_redo("root1", "sha2", "stash")
    await engine.handle_checkpoint_rollback("root1", "sha3")
    await engine.handle_checkpoint_roll_forward("root1", "sha4")
    await engine.handle_checkpoint_list("root1")

    assert [c[0] for c in calls] == ["undo", "redo", "rollback", "roll_forward", "state_for"]
    assert calls[1][1] == ("root1", "sha2", "stash")


# ---------------------------------------------------------------------------
# _root_paths
# ---------------------------------------------------------------------------


def test_root_paths_guided_mode_reports_bound_project(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "guided"
    engine._current_project = {"root": "/proj", "name": "proj"}

    paths = engine._root_paths()

    assert len(paths) == 1
    assert paths[0].name == "proj"
    assert paths[0].path == "/proj"


def test_root_paths_problem_solving_reports_workspace_folders(tmp_path: Path) -> None:
    folder = tmp_path / "a"
    folder.mkdir()
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "problem_solving"
    engine._session_workspace.set_folders({"a": folder})

    paths = engine._root_paths()

    assert len(paths) == 1
    assert paths[0].name == "a"


def test_root_paths_empty_when_no_folders(tmp_path: Path) -> None:
    """No fallback to physical_root: a homeless session reports zero roots,
    so ``RootMirrorManager`` never gets handed a root to mirror (doc: the
    checkpoint jail-escape fix — physical_root must never leak in here)."""
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)
    engine._session.workflow_mode = "problem_solving"

    paths = engine._root_paths()

    assert paths == ()


# ---------------------------------------------------------------------------
# _util_paths
# ---------------------------------------------------------------------------


def test_util_paths_empty_when_nothing_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_core, "kodo_user_dir", lambda: tmp_path / "home")
    engine, _t, _s, _g = _make_engine(tmp_path)
    assert engine._util_paths() == {}


def test_util_paths_includes_found_utils(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_find_util(kodo_dir, name):
        if name == "fd":
            return SimpleNamespace(path=Path("/opt/fd"))
        return None

    monkeypatch.setattr(_core, "find_util", _fake_find_util)
    assert WorkflowEngine._util_paths() == {"fd": Path("/opt/fd")}


# ---------------------------------------------------------------------------
# _make_resolver
# ---------------------------------------------------------------------------


def test_make_resolver_guided_mode_with_layout_uses_project_resolver(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "guided"

    from kodo.project import ProjectLayout

    engine._layout = ProjectLayout(project_root)

    resolver = engine._make_resolver("sess-1")
    from kodo.tools import ProjectPathResolver

    assert isinstance(resolver, ProjectPathResolver)


def test_make_resolver_falls_back_to_logical_without_layout(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "guided"
    engine._layout = None

    resolver = engine._make_resolver("sess-1")
    from kodo.tools import LogicalPathResolver

    assert isinstance(resolver, LogicalPathResolver)


def test_make_resolver_problem_solving_uses_logical_resolver(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "problem_solving"

    resolver = engine._make_resolver("sess-1")
    from kodo.tools import LogicalPathResolver

    assert isinstance(resolver, LogicalPathResolver)


def test_make_resolver_logical_resolver_tracks_folders_added_after_construction(
    tmp_path: Path,
) -> None:
    """A resolver built at the start of a turn must still see a project bound
    *after* it was constructed — e.g. by ``create_new_project`` mid-turn, or
    by the user adding a folder to the VS Code window directly (both funnel
    into ``SessionWorkspace.set_folders``). Regression test: the resolver used
    to snapshot the folder map at construction time, so a project bound later
    in the same turn was invisible to it until the *next* turn rebuilt it."""
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "problem_solving"
    resolver = engine._make_resolver("sess-1")

    project_root = tmp_path / "proj"
    project_root.mkdir()
    with pytest.raises(PermissionError):
        resolver.resolve("proj/foo.py")

    engine._session_workspace.set_folders({"proj": project_root})

    assert resolver.resolve("proj/foo.py") == (project_root / "foo.py").resolve()


# ---------------------------------------------------------------------------
# _run_rollback
# ---------------------------------------------------------------------------


async def test_run_rollback_resets_session_state(tmp_path: Path) -> None:
    from kodo.llms import Message

    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, _t, _s, _g = _make_engine(tmp_path)
    await engine._bind_project(str(project_root), "proj", emit=False)
    engine._main_messages = [Message(role="user", content="stale")]
    engine._replay_subsessions = [{"subsession_id": "x"}]

    rollback_calls = []

    class _FakeMirrors:
        def set_roots(self, roots) -> None:
            pass

        async def rollback(self, root, sha):
            rollback_calls.append((root, sha))

    engine._checkpoints._mirrors = _FakeMirrors()

    await engine._run_rollback("deadbeef")

    assert rollback_calls == [(str(project_root.resolve()), "deadbeef")]
    assert engine._main_messages == []
    assert engine._replay_subsessions is None


# ---------------------------------------------------------------------------
# _finalize_document
# ---------------------------------------------------------------------------


async def test_finalize_document_unresolvable_path_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmp_path lives under the OS temp dir, so an escape via ".." would land
    # inside the (intentionally allowed) system-temp carve-out — blank it out
    # to isolate the "escapes the project root" guard this test targets.
    monkeypatch.setattr("kodo.tools._paths.system_temp_roots", lambda: ())
    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, _t, _s, _g = _make_engine(tmp_path)
    await engine._bind_project(str(project_root), "proj", emit=False)

    await engine._finalize_document("../../etc/passwd")  # escapes the project root


def _seed_tracked_doc(project_root: Path, rel_path: str) -> Path:
    from kodo.guided_state import append_new_revision

    doc = project_root / rel_path
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("content", encoding="utf-8")
    append_new_revision(
        doc,
        project_root,
        commit_hash="sha-1",
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    return doc


async def test_finalize_document_autonomous_auto_accepts(tmp_path: Path) -> None:
    from kodo.guided_state import read_history

    project_root = tmp_path / "proj"
    _make_project(project_root)
    engine, _t, _s, gate = _make_engine(tmp_path)
    await engine._bind_project(str(project_root), "proj", emit=False)
    engine._session.effective_autonomous = True
    _seed_tracked_doc(project_root, "specs/a.md")

    await engine._finalize_document("specs/a.md")

    assert gate.calls == []
    history = read_history(project_root / "specs" / "a.md", project_root)
    assert [e["type"] for e in history] == ["new_revision", "accepted"]


async def test_finalize_document_interactive_agree_accepts(tmp_path: Path) -> None:
    from kodo.guided_state import read_history

    project_root = tmp_path / "proj"
    _make_project(project_root)
    gate = _FakeGate(action="agree")
    engine, _t, _s, _g = _make_engine(tmp_path, gate=gate)
    await engine._bind_project(str(project_root), "proj", emit=False)
    engine._session.effective_autonomous = False
    _seed_tracked_doc(project_root, "specs/a.md")

    await engine._finalize_document("specs/a.md")

    assert len(gate.calls) == 1
    history = read_history(project_root / "specs" / "a.md", project_root)
    assert [e["type"] for e in history] == ["new_revision", "review_result", "accepted"]


async def test_finalize_document_interactive_feedback_rejects(tmp_path: Path) -> None:
    from kodo.guided_state import read_history

    project_root = tmp_path / "proj"
    _make_project(project_root)
    gate = _FakeGate(action="feedback", feedback="needs work")
    engine, _t, _s, _g = _make_engine(tmp_path, gate=gate)
    await engine._bind_project(str(project_root), "proj", emit=False)
    engine._session.effective_autonomous = False
    _seed_tracked_doc(project_root, "specs/a.md")

    await engine._finalize_document("specs/a.md")

    history = read_history(project_root / "specs" / "a.md", project_root)
    assert [e["type"] for e in history] == ["new_revision", "review_result"]
    assert history[-1]["comment"] == "needs work"


# ---------------------------------------------------------------------------
# full_history
# ---------------------------------------------------------------------------


async def test_full_history_forwards_to_projector(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)

    async def _fake_full_history():
        return {
            "entries": [{"type": "user_message", "content": "hi", "attachments": []}],
            "subsessions": {},
        }

    engine._history.full_history = _fake_full_history

    history = await engine.full_history()
    assert history == {
        "entries": [{"type": "user_message", "content": "hi", "attachments": []}],
        "subsessions": {},
    }


# ---------------------------------------------------------------------------
# _disable_autonomous
# ---------------------------------------------------------------------------


async def test_disable_autonomous_clears_both_flags_and_notifies(tmp_path: Path) -> None:
    engine, transient, sink, _g = _make_engine(tmp_path)
    transient.attach_session("s1", resumed=False)
    engine._session.autonomous = True
    engine._session.effective_autonomous = True

    await engine._disable_autonomous()

    assert engine._session.autonomous is False
    assert engine._session.effective_autonomous is False
    assert transient.autonomous is False
    revoke_events = [e for e in sink.sent if e.payload.get("type") == "autonomous.changed"]
    assert revoke_events[0].payload["autonomous"] is False


# ---------------------------------------------------------------------------
# handle_project_create / _create_project / _reserve_project_dir
# ---------------------------------------------------------------------------


async def test_create_project_with_explicit_path(tmp_path: Path) -> None:
    engine, _t, sink, _g = _make_engine(tmp_path)
    target = tmp_path / "explicit-proj"

    result = await engine.handle_project_create(name="My Project", path=str(target))

    assert result == {"path": str(target), "name": "My Project"}
    assert (target / ".kodo" / "kodo.md").exists()
    assert "My Project" in engine._session_workspace.folders
    add_folder_events = [e for e in sink.sent if e.payload.get("type") == "workspace.add_folder"]
    assert len(add_folder_events) == 1


async def test_create_project_without_path_slugifies_name(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)

    result = await engine._create_project("My Cool Project")

    # The human-readable label is preserved; only the on-disk dir is slugified.
    assert result["name"] == "My Cool Project"
    assert Path(result["path"]).name == "my-cool-project"
    assert Path(result["path"]).exists()
    assert (Path(result["path"]) / ".kodo" / "kodo.md").exists()


async def test_create_project_requires_name_or_path(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    with pytest.raises(ValueError, match="requires a non-empty"):
        await engine._create_project("")


async def test_create_project_label_falls_back_to_dir_name_on_collision(tmp_path: Path) -> None:
    (tmp_path / "dup").mkdir()  # forces the reserved dir to be "dup-2"
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)
    engine._session_workspace.set_folders({"dup": tmp_path / "existing"})

    result = await engine._create_project("dup")

    # "dup" is already a folder label, so the new one falls back to its dir name.
    assert result["name"] == "dup-2"


def test_reserve_project_dir_creates_and_returns_unique_path(tmp_path: Path) -> None:
    (tmp_path / "widget").mkdir()
    result = WorkflowEngine._reserve_project_dir(tmp_path, "widget")
    assert result == tmp_path / "widget-2"
    assert result.exists()


# ---------------------------------------------------------------------------
# _init_project
# ---------------------------------------------------------------------------


async def test_init_project_scaffolds_empty_directory(tmp_path: Path) -> None:
    engine, _t, sink, _g = _make_engine(tmp_path)
    target = tmp_path / "existing-empty"
    target.mkdir()

    result = await engine._init_project(str(target))

    assert result == {"path": str(target), "name": "existing-empty", "scaffolded": True}
    assert (target / "specs").is_dir()
    assert (target / "src").is_dir()
    assert (target / "test").is_dir()
    assert (target / ".kodo" / "kodo.md").exists()
    # The checkpoint mirror was git-initialised with its baseline commit.
    assert (target / ".kodo" / "checkpoints" / ".git").exists()
    assert "existing-empty" in engine._session_workspace.folders
    add_folder_events = [e for e in sink.sent if e.payload.get("type") == "workspace.add_folder"]
    assert len(add_folder_events) == 1


async def test_init_project_treats_dotfiles_only_as_empty(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    target = tmp_path / "existing-dotfiles-only"
    target.mkdir()
    (target / ".git").mkdir()
    (target / ".gitignore").write_text("node_modules/\n")

    result = await engine._init_project(str(target))

    assert result["scaffolded"] is True
    assert (target / "specs").is_dir()
    assert (target / "src").is_dir()
    assert (target / "test").is_dir()


async def test_init_project_preserves_existing_content(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    target = tmp_path / "existing-with-content"
    target.mkdir()
    (target / "README.md").write_text("hello\n")

    result = await engine._init_project(str(target))

    assert result["scaffolded"] is False
    assert not (target / "specs").exists()
    assert not (target / "src").exists()
    assert not (target / "test").exists()
    assert (target / "README.md").read_text() == "hello\n"
    assert (target / ".kodo" / "kodo.md").exists()


async def test_init_project_requires_existing_directory(tmp_path: Path) -> None:
    from kodo.project import ProjectLayoutError

    engine, _t, _s, _g = _make_engine(tmp_path)
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ProjectLayoutError, match="does not exist"):
        await engine._init_project(str(missing))


async def test_init_project_fails_when_kodo_dir_already_exists(tmp_path: Path) -> None:
    from kodo.project import ProjectLayoutError

    engine, _t, _s, _g = _make_engine(tmp_path)
    target = tmp_path / "already-a-project"
    (target / ".kodo").mkdir(parents=True)

    with pytest.raises(ProjectLayoutError, match="already exists"):
        await engine._init_project(str(target))


# ---------------------------------------------------------------------------
# _has_workspace
# ---------------------------------------------------------------------------


def test_has_workspace_guided_false_until_project_bound(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "guided"
    assert engine._has_workspace() is False
    engine._current_project = {"root": str(tmp_path), "name": "proj"}
    assert engine._has_workspace() is True


def test_has_workspace_problem_solving_tracks_folders(tmp_path: Path) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path)
    engine._session.workflow_mode = "problem_solving"
    assert engine._has_workspace() is False
    engine._session_workspace.set_folders({"proj": tmp_path})
    assert engine._has_workspace() is True


# ---------------------------------------------------------------------------
# _bootstrap_project / _bootstrap_project_autonomous / _bootstrap_project_interactive
# ---------------------------------------------------------------------------


async def test_bootstrap_project_autonomous_uses_titler_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)
    engine._session.autonomous = True
    engine._session.effective_autonomous = True
    engine._current_prompt_text = "build me a todo list app"
    home = tmp_path / "home-dir"
    monkeypatch.setattr(_core.Path, "home", staticmethod(lambda: home))

    async def _fake_generate(text: str) -> str:
        assert text == "build me a todo list app"
        return "Todo App"

    monkeypatch.setattr(_core, "generate_project_name", _fake_generate)

    result = await engine._bootstrap_project()

    assert result["name"] == "Todo App"
    assert Path(result["path"]) == home / "kodo-projects" / "todo-app"
    assert (Path(result["path"]) / ".kodo" / "kodo.md").exists()
    assert engine._session_workspace.physical_root == (home / "kodo-projects").resolve()


async def test_bootstrap_project_autonomous_falls_back_to_generic_name_on_titler_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)
    engine._session.autonomous = True
    engine._session.effective_autonomous = True
    engine._current_prompt_text = "anything"
    home = tmp_path / "home-dir"
    monkeypatch.setattr(_core.Path, "home", staticmethod(lambda: home))

    async def _fake_generate(text: str) -> None:
        return None

    monkeypatch.setattr(_core, "generate_project_name", _fake_generate)

    result = await engine._bootstrap_project()

    assert result["name"] == "project"
    assert Path(result["path"]) == home / "kodo-projects" / "project"


async def test_bootstrap_project_autonomous_prefers_agent_supplied_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent-supplied ``name`` is used as-is; the titler is never consulted."""
    engine, _t, _s, _g = _make_engine(tmp_path, physical_root=tmp_path)
    engine._session.autonomous = True
    engine._session.effective_autonomous = True
    home = tmp_path / "home-dir"
    monkeypatch.setattr(_core.Path, "home", staticmethod(lambda: home))

    async def _fake_generate(text: str) -> str:
        raise AssertionError("titler should not be consulted when name is given")

    monkeypatch.setattr(_core, "generate_project_name", _fake_generate)

    result = await engine._bootstrap_project("Tic Tac Toe")

    assert result["name"] == "Tic Tac Toe"
    assert Path(result["path"]) == home / "kodo-projects" / "tic-tac-toe"


async def test_bootstrap_project_interactive_creates_named_subdir_under_picked_folder(
    tmp_path: Path,
) -> None:
    class _FolderGate(_FakeGate):
        async def fire_choose_project_folder(self):  # noqa: ANN201
            from kodo.runtime._gates import ChooseFolderResponse

            return ChooseFolderResponse(path=str(tmp_path / "picked"))

    engine, _t, _s, _g = _make_engine(tmp_path, gate=_FolderGate())
    engine._session.autonomous = False
    engine._session.effective_autonomous = False

    result = await engine._bootstrap_project("Tic Tac Toe")

    assert result["path"] == str(tmp_path / "picked" / "tic-tac-toe")
    assert (tmp_path / "picked" / "tic-tac-toe" / ".kodo" / "kodo.md").exists()
    # The picked folder itself is never scaffolded as a project — only used
    # as the parent (Reading 2: sibling projects, never nested).
    assert not (tmp_path / "picked" / ".kodo").exists()
    assert engine._session_workspace.physical_root == (tmp_path / "picked").resolve()


async def test_bootstrap_project_interactive_cancelled_returns_error(tmp_path: Path) -> None:
    class _FolderGate(_FakeGate):
        async def fire_choose_project_folder(self):  # noqa: ANN201
            from kodo.runtime._gates import ChooseFolderResponse

            return ChooseFolderResponse(path="", error="cancelled")

    engine, _t, _s, _g = _make_engine(tmp_path, gate=_FolderGate())
    engine._session.autonomous = False
    engine._session.effective_autonomous = False

    result = await engine._bootstrap_project()

    assert result == {"error": "cancelled"}


async def test_init_project_skips_workspace_add_when_already_present(tmp_path: Path) -> None:
    engine, _t, sink, _g = _make_engine(tmp_path)
    target = tmp_path / "already-open"
    target.mkdir()
    engine._session_workspace.set_folders({"already-open": target})

    result = await engine._init_project(str(target))

    assert result["name"] == "already-open"
    add_folder_events = [e for e in sink.sent if e.payload.get("type") == "workspace.add_folder"]
    assert len(add_folder_events) == 0
    # No duplicate entry was created under a different label.
    assert list(engine._session_workspace.folders) == ["already-open"]

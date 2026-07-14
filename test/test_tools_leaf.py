"""Behavior tests for the leaf-agent tools in :mod:`kodo.tools`.

Every agent shares one :class:`~kodo.tools.ToolDispatcher`; these tests
exercise the tools a leaf sub-agent typically holds — the file-evolution tools
(``read_file``, ``document_feedback``), ``escalate_blocker``, and the native
file-I/O / shell tools — plus the shared ``tools_for_agent`` resolver.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.guided_state import read_status
from kodo.runtime import GateOrchestrator, SessionState
from kodo.tools import (
    DISPATCHABLE_TOOLS_BY_NAME,
    ProjectPathResolver,
    ToolDispatcher,
    tools_for_agent,
)
from kodo.toolspecs import DOCUMENT_FEEDBACK, READ_FILE, requires_intent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state() -> MagicMock:
    state = MagicMock()
    state.send = AsyncMock()
    captured: dict[str, object] = {}
    state.register_response_future = lambda req_id, f: captured.update({req_id: f})
    state._captured = captured
    return state


def _make_gate(answer: str = "") -> GateOrchestrator:
    """Return a GateOrchestrator whose fire_questions resolves every question
    to the given free-text answer."""
    gate = GateOrchestrator(_make_app_state(), MagicMock())

    async def _instant_questions(
        questions: list[dict[str, object]], tool_call_id: str = ""
    ) -> list[dict[str, object]]:
        return [{"selected": [], "free_text": answer or None} for _ in questions]

    gate.fire_questions = _instant_questions  # type: ignore[method-assign]
    return gate


class _StubServices:
    """Engine-side stub satisfying ``kodo.tools.EngineServices``.

    None of the leaf tools under test invoke these; they exist only to
    satisfy the protocol.
    """

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        return {}

    async def run_dependency_manager(self, task_input: dict[str, object]) -> dict[str, object]:
        return {"status": "completed", "summary": "done"}

    async def run_web_summarizer(self, task_input: dict[str, object]) -> dict[str, object]:
        return {"themes": []}

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        path: str,
        input_paths: dict[str, str],
        instructions: str,
        for_revision: bool,
    ) -> dict[str, object]:
        return {"path": path, "status": "accepted", "concerns": []}

    async def rollback(self, target_sha: str) -> None:
        return None

    async def disable_autonomous_mode(self) -> None:
        return None

    async def create_project(
        self, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        return {"path": path or f"/tmp/{name}", "name": name}

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        return None


_TEST_INTENT = "exercise this tool in a behavior test"


class _IntentDispatcher(ToolDispatcher):
    """Injects a default ``intent`` so cases stay focused on their own behavior.

    The dispatcher-level intent enforcement itself is covered by the dedicated
    intent tests below (which call ``ToolDispatcher.dispatch`` directly).
    """

    async def dispatch(
        self, tool_name: str, tool_input: dict[str, object], tool_use_id: str = ""
    ) -> str:
        spec = DISPATCHABLE_TOOLS_BY_NAME.get(tool_name)
        if spec is not None and requires_intent(spec) and "intent" not in tool_input:
            tool_input = {"intent": _TEST_INTENT, **tool_input}
        return await super().dispatch(tool_name, tool_input, tool_use_id)


def _make_dispatcher(
    tmp_path: Path,
    agent_name: str = "test_agent",
    answer: str = "",
    autonomous: bool = False,
    mode: str = "guided",
) -> ToolDispatcher:
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous

    return _IntentDispatcher(
        resolver=ProjectPathResolver(tmp_path),
        gate=_make_gate(answer),
        session=session,
        services=_StubServices(),
        agent_name=agent_name,
        session_id="sess-test",
        mode=mode,
        project_root=tmp_path,
    )


# ---------------------------------------------------------------------------
# Tool spec constants & catalog
# ---------------------------------------------------------------------------


def test_read_file_spec_has_correct_name() -> None:
    assert READ_FILE.name == "read_file"


def test_document_feedback_spec_has_correct_name() -> None:
    assert DOCUMENT_FEEDBACK.name == "document_feedback"


def test_dispatchable_catalog_includes_file_evolution_tools() -> None:
    for name in ("read_file", "document_feedback", "escalate_blocker", "ask_user"):
        assert name in DISPATCHABLE_TOOLS_BY_NAME


def test_dispatchable_catalog_includes_fileio_and_shell_tools() -> None:
    for name in (
        "filesystem",
        "edit_file",
        "create_directory",
        "run_command",
    ):
        assert name in DISPATCHABLE_TOOLS_BY_NAME


# ---------------------------------------------------------------------------
# tools_for_agent (takes tool names, not a SubAgent)
# ---------------------------------------------------------------------------


def test_tools_for_agent_returns_specs_for_declared_tools() -> None:
    result = tools_for_agent(frozenset(["read_file", "document_feedback"]))
    names = {t.name for t in result}
    assert names == {"read_file", "document_feedback"}


def test_tools_for_agent_skips_unknown_tool_names() -> None:
    result = tools_for_agent(frozenset(["read_file", "nonexistent_tool"]))
    names = {t.name for t in result}
    assert "read_file" in names
    assert "nonexistent_tool" not in names


# ---------------------------------------------------------------------------
# read_file dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_returns_whole_file(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("line1\nline2\nline3", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("read_file", {"path": "a.md"}))
    assert result["total_lines"] == 3
    assert result["sections"][0]["content"] == "line1\nline2\nline3"


@pytest.mark.asyncio
async def test_read_file_returns_requested_range(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("line1\nline2\nline3", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "read_file", {"path": "a.md", "ranges": [{"start_line": 2, "end_line": 2}]}
        )
    )
    assert result["sections"] == [{"start_line": 2, "end_line": 2, "content": "line2"}]


@pytest.mark.asyncio
async def test_read_file_missing_file_returns_error(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("read_file", {"path": "missing.md"}))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_file_rejects_ranges_and_pattern_together(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "read_file",
            {"path": "a.md", "ranges": [{"start_line": 1, "end_line": 1}], "pattern": "x"},
        )
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# document_feedback dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_feedback_records_rejection(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path, agent_name="architect_critic")
    result = json.loads(
        await dispatcher.dispatch(
            "document_feedback",
            {
                "path": "specs/a.md",
                "accept": False,
                "concerns": [{"kind": "gap", "description": "missing section"}],
            },
        )
    )
    assert result == {"status": "recorded", "path": "specs/a.md"}
    status = read_status(tmp_path / "specs" / "a.md", tmp_path)
    assert status is not None
    assert status["status"] == "needs_revision"
    assert status["reviewer"] == "architect_critic"


@pytest.mark.asyncio
async def test_document_feedback_rejects_empty_concerns(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path, agent_name="architect_critic")
    result = json.loads(
        await dispatcher.dispatch(
            "document_feedback", {"path": "specs/a.md", "accept": False, "concerns": []}
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_document_feedback_accept_records_pending_acceptance(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path, agent_name="architect_critic")
    result = json.loads(
        await dispatcher.dispatch("document_feedback", {"path": "specs/a.md", "accept": True})
    )
    assert result["status"] == "recorded"
    status = read_status(tmp_path / "specs" / "a.md", tmp_path)
    assert status is not None
    assert status["status"] == "pending_acceptance"


# ---------------------------------------------------------------------------
# escalate_blocker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_blocker_sets_stop_flag(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, answer="user reply")
    result = json.loads(
        await dispatcher.dispatch(
            "escalate_blocker", {"reason": "cap_reached", "summary": "Need help"}
        )
    )
    assert result["status"] == "escalated"
    assert "user_response" in result
    assert dispatcher.stop_requested


# ---------------------------------------------------------------------------
# submit_evaluation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_evaluation_records_and_stops(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("submit_evaluation", {"score": 87, "report": "solid; minor gaps"})
    )
    assert result["status"] == "recorded"
    assert result["score"] == 87.0
    assert result["report"] == "solid; minor gaps"
    assert dispatcher.stop_requested


@pytest.mark.asyncio
async def test_submit_evaluation_clamps_and_coerces_score(tmp_path: Path) -> None:
    over = json.loads(
        await _make_dispatcher(tmp_path).dispatch(
            "submit_evaluation", {"score": 150, "report": "x"}
        )
    )
    assert over["score"] == 100.0

    under = json.loads(
        await _make_dispatcher(tmp_path).dispatch("submit_evaluation", {"score": -5, "report": "x"})
    )
    assert under["score"] == 0.0

    stringy = json.loads(
        await _make_dispatcher(tmp_path).dispatch(
            "submit_evaluation", {"score": "72", "report": "x"}
        )
    )
    assert stringy["score"] == 72.0

    junk = json.loads(
        await _make_dispatcher(tmp_path).dispatch(
            "submit_evaluation", {"score": "not-a-number", "report": ""}
        )
    )
    assert junk["score"] == 0.0
    assert junk["report"] == ""


# ---------------------------------------------------------------------------
# Native file I/O tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_file_writes_new_file(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("create_file", {"path": "out.txt", "content": "hello"})
    )
    assert result["status"] == "created"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_create_file_fails_if_already_exists(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("existing", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("create_file", {"path": "out.txt", "content": "hello"})
    )
    assert "error" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "existing"


@pytest.mark.asyncio
async def test_create_directory_makes_parents(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("create_directory", {"path": "a/b/c"}))
    assert result["status"] == "created"
    assert (tmp_path / "a" / "b" / "c").is_dir()


@pytest.mark.asyncio
async def test_create_directory_succeeds_if_already_exists(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("create_directory", {"path": "a"}))
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_filesystem_unknown_operation_errors(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("filesystem", {"operation": "frobnicate"}))
    assert "error" in result


@pytest.mark.asyncio
async def test_edit_file_replaces_unique_match(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("alpha beta gamma", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {"path": "out.txt", "old_string": "beta", "new_string": "BETA"},
        )
    )
    assert result["status"] == "edited"
    # Only the matched snippet changes; everything else is preserved byte-for-byte.
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "alpha BETA gamma"


@pytest.mark.asyncio
async def test_edit_file_fails_when_match_missing(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("hello world", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {"path": "out.txt", "old_string": "nope", "new_string": "x"},
        )
    )
    assert "error" in result
    # Nothing is written on a failed match.
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello world"


@pytest.mark.asyncio
async def test_edit_file_fails_when_match_not_unique(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("x x x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {"path": "out.txt", "old_string": "x", "new_string": "y"},
        )
    )
    assert "error" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x x x"


@pytest.mark.asyncio
async def test_edit_file_fails_if_missing(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {"path": "missing.txt", "old_string": "a", "new_string": "b"},
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_delete_file_removes_file(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("content", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("filesystem", {"operation": "delete_file", "path": "out.txt"})
    )
    assert result["status"] == "deleted"
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_delete_file_rejects_directory(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("filesystem", {"operation": "delete_file", "path": "dir"})
    )
    assert "error" in result
    assert (tmp_path / "dir").is_dir()


@pytest.mark.asyncio
async def test_copy_file_copies_content(tmp_path: Path) -> None:
    (tmp_path / "src.txt").write_text("content", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem",
            {"operation": "copy_file", "source": "src.txt", "destination": "dst.txt"},
        )
    )
    assert result["status"] == "copied"
    assert (tmp_path / "src.txt").read_text(encoding="utf-8") == "content"
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
async def test_move_file_renames_file(tmp_path: Path) -> None:
    (tmp_path / "src.txt").write_text("content", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem",
            {"operation": "move_file", "source": "src.txt", "destination": "dst.txt"},
        )
    )
    assert result["status"] == "moved"
    assert not (tmp_path / "src.txt").exists()
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "content"


# ---------------------------------------------------------------------------
# Native directory operations (filesystem tool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_dir_removes_tree(tmp_path: Path) -> None:
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "sub" / "f.txt").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("filesystem", {"operation": "delete_dir", "path": "d"})
    )
    assert result["status"] == "deleted"
    assert not (tmp_path / "d").exists()


@pytest.mark.asyncio
async def test_delete_dir_rejects_file(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("filesystem", {"operation": "delete_dir", "path": "f.txt"})
    )
    assert "error" in result
    assert (tmp_path / "f.txt").exists()


@pytest.mark.asyncio
async def test_copy_dir_copies_tree(tmp_path: Path) -> None:
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "sub" / "f.txt").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem", {"operation": "copy_dir", "source": "src", "destination": "dst"}
        )
    )
    assert result["status"] == "copied"
    assert (tmp_path / "dst" / "sub" / "f.txt").read_text(encoding="utf-8") == "x"
    assert (tmp_path / "src" / "sub" / "f.txt").exists()


@pytest.mark.asyncio
async def test_copy_dir_fails_if_destination_exists(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem", {"operation": "copy_dir", "source": "src", "destination": "dst"}
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_move_dir_relocates_tree(tmp_path: Path) -> None:
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "sub" / "f.txt").write_text("x", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem", {"operation": "move_dir", "source": "src", "destination": "dst"}
        )
    )
    assert result["status"] == "moved"
    assert not (tmp_path / "src").exists()
    assert (tmp_path / "dst" / "sub" / "f.txt").read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_fileio_rejects_path_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmp_path itself lives under the OS temp dir, so a plain ".." escape
    # would land inside the (intentionally allowed) system-temp carve-out —
    # blank it out here to isolate the traversal guard from that carve-out.
    monkeypatch.setattr("kodo.tools._paths.system_temp_roots", lambda: ())
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("create_file", {"path": "../escape.txt", "content": "nope"})
    )
    assert "error" in result
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_fileio_allows_path_under_system_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    monkeypatch.setattr("kodo.tools._paths.system_temp_roots", lambda: (str(scratch_dir),))
    dispatcher = _make_dispatcher(project_root)
    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"path": str(scratch_dir / "note.txt"), "content": "hi"}
        )
    )
    assert result.get("status") == "created"
    assert (scratch_dir / "note.txt").read_text(encoding="utf-8") == "hi"


# ---------------------------------------------------------------------------
# `temporary`: session-scoped scratch directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_file_temporary_resolves_under_session_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_root = tmp_path / "scratch"
    monkeypatch.setattr("kodo.tools._tool.session_temp_dir", lambda session_id: scratch_root)
    dispatcher = _make_dispatcher(project_root)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"path": "note.txt", "content": "hi", "temporary": True}
        )
    )

    assert result["status"] == "created"
    assert (scratch_root / "note.txt").read_text(encoding="utf-8") == "hi"
    assert not (project_root / "note.txt").exists()


@pytest.mark.asyncio
async def test_create_file_without_temporary_still_resolves_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_root = tmp_path / "scratch"
    monkeypatch.setattr("kodo.tools._tool.session_temp_dir", lambda session_id: scratch_root)
    dispatcher = _make_dispatcher(tmp_path)

    result = json.loads(
        await dispatcher.dispatch("create_file", {"path": "note.txt", "content": "hi"})
    )

    assert result["status"] == "created"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hi"
    assert not scratch_root.exists()


@pytest.mark.asyncio
async def test_create_directory_temporary_resolves_under_session_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_root = tmp_path / "scratch"
    monkeypatch.setattr("kodo.tools._tool.session_temp_dir", lambda session_id: scratch_root)
    dispatcher = _make_dispatcher(project_root)

    result = json.loads(
        await dispatcher.dispatch("create_directory", {"path": "sub", "temporary": True})
    )

    assert result["status"] == "created"
    assert (scratch_root / "sub").is_dir()
    assert not (project_root / "sub").exists()


@pytest.mark.asyncio
async def test_edit_file_temporary_resolves_under_session_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    (scratch_root / "out.txt").write_text("alpha beta gamma", encoding="utf-8")
    monkeypatch.setattr("kodo.tools._tool.session_temp_dir", lambda session_id: scratch_root)
    dispatcher = _make_dispatcher(project_root)

    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {
                "path": "out.txt",
                "old_string": "beta",
                "new_string": "BETA",
                "temporary": True,
            },
        )
    )

    assert result["status"] == "edited"
    assert (scratch_root / "out.txt").read_text(encoding="utf-8") == "alpha BETA gamma"


@pytest.mark.asyncio
async def test_filesystem_delete_dir_temporary_resolves_under_session_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_root = tmp_path / "scratch"
    (scratch_root / "d").mkdir(parents=True)
    monkeypatch.setattr("kodo.tools._tool.session_temp_dir", lambda session_id: scratch_root)
    dispatcher = _make_dispatcher(project_root)

    result = json.loads(
        await dispatcher.dispatch(
            "filesystem", {"operation": "delete_dir", "path": "d", "temporary": True}
        )
    )

    assert result["status"] == "deleted"
    assert not (scratch_root / "d").exists()


@pytest.mark.asyncio
async def test_temporary_still_rejects_escape_outside_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same isolation as test_fileio_rejects_path_outside_project_root: blank
    # out the OS-temp carve-out so it can't mask the containment guard, since
    # tmp_path (and thus scratch_root) already lives under the OS temp dir.
    monkeypatch.setattr("kodo.tools._paths.system_temp_roots", lambda: ())
    scratch_root = tmp_path / "scratch"
    monkeypatch.setattr("kodo.tools._tool.session_temp_dir", lambda session_id: scratch_root)
    dispatcher = _make_dispatcher(tmp_path / "project")

    result = json.loads(
        await dispatcher.dispatch(
            "create_file",
            {"path": "../escape.txt", "content": "nope", "temporary": True},
        )
    )

    assert "error" in result
    assert not (tmp_path / "escape.txt").exists()


# ---------------------------------------------------------------------------
# Native shell tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_returns_exit_code_and_output(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("run_command", {"command": "echo hello", "timeout": 10})
    )
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_run_command_rejects_working_dir_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # See test_fileio_rejects_path_outside_project_root: tmp_path sits under
    # the OS temp dir, so blank out the carve-out to isolate the guard.
    monkeypatch.setattr("kodo.tools._paths.system_temp_roots", lambda: ())
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "run_command", {"command": "pwd", "working_dir": "..", "timeout": 10}
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_run_command_allows_working_dir_under_system_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    monkeypatch.setattr("kodo.tools._paths.system_temp_roots", lambda: (str(scratch_dir),))
    dispatcher = _make_dispatcher(project_root)
    result = json.loads(
        await dispatcher.dispatch(
            "run_command",
            {"command": "pwd", "working_dir": str(scratch_dir), "timeout": 10},
        )
    )
    assert "error" not in result
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_command_requires_timeout(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("run_command", {"command": "echo hi"}))
    assert "error" in result and "timeout" in result["error"].lower()


@pytest.mark.asyncio
async def test_run_command_kills_on_timeout(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("run_command", {"command": "sleep 5", "timeout": 0.2})
    )
    assert result["exit_code"] is None
    assert "timed out" in result["stderr"].lower()


@pytest.mark.asyncio
async def test_run_command_timeout_kills_backgrounded_child(tmp_path: Path) -> None:
    # Regression: a command that backgrounds a long-lived child which inherits
    # the stdout/stderr pipes used to wedge the post-kill drain forever (killing
    # only the wrapping shell left the grandchild holding the pipes open). With
    # process-group kill + a bounded drain, the call must still return promptly.
    import time

    dispatcher = _make_dispatcher(tmp_path)
    start = time.monotonic()
    result = json.loads(
        await dispatcher.dispatch(
            "run_command",
            {"command": "sleep 30 & echo started; sleep 30", "timeout": 0.3},
        )
    )
    elapsed = time.monotonic() - start
    assert result["exit_code"] is None
    assert "timed out" in result["stderr"].lower()
    # Must unblock well within the backgrounded child's 30s lifetime
    # (timeout 0.3s + bounded 5s drain, with generous slack).
    assert elapsed < 15


@pytest.mark.asyncio
async def test_run_command_closes_stdin(tmp_path: Path) -> None:
    # A command that reads stdin must get immediate EOF, not hang on the
    # server's inherited stdin. `cat` with no file reads stdin until EOF.
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("run_command", {"command": "cat", "timeout": 5}))
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("no_such_tool", {}))
    assert "error" in result


# ---------------------------------------------------------------------------
# intent enforcement (first-degree mutators)
# ---------------------------------------------------------------------------

# Every tool whose own dispatch mutates content on disk; second-degree
# mutators (run_subagent, run_author_critic_iteration, toolchain_deps) and
# toolchain_build (runs the project's own generated scripts) are exempt.
_INTENT_TOOLS = (
    "filesystem",
    "edit_file",
    "create_file",
    "create_directory",
    "run_command",
    "create_new_project",
    "rollback",
)


def test_mutating_specs_declare_intent_first_required_and_visible() -> None:
    for name in _INTENT_TOOLS:
        spec = DISPATCHABLE_TOOLS_BY_NAME[name]
        assert requires_intent(spec), name
        props = spec.input_schema["properties"]
        assert isinstance(props, dict), name
        # `intent` is the TOP field — first in the schema, so first in the
        # tool-call detail box — and always shown.
        assert next(iter(props)) == "intent", name
        assert spec.input_visibility.get("intent") == "always", name


def test_non_mutating_and_second_degree_tools_do_not_require_intent() -> None:
    for name in DISPATCHABLE_TOOLS_BY_NAME:
        if name not in _INTENT_TOOLS:
            assert not requires_intent(DISPATCHABLE_TOOLS_BY_NAME[name]), name


@pytest.mark.asyncio
async def test_missing_or_blank_intent_is_rejected_before_dispatch(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    for extra in ({}, {"intent": "   "}, {"intent": 7}):
        payload: dict[str, object] = {
            **extra,
            "path": "never.txt",
            "content": "hi",
        }
        # Call the real ToolDispatcher.dispatch, bypassing the test-only
        # intent injection, to exercise the generic enforcement.
        result = json.loads(await ToolDispatcher.dispatch(dispatcher, "create_file", payload))
        assert "intent" in result["error"]
        # Rejected before the handler ran — nothing was written.
        assert not (tmp_path / "never.txt").exists()


@pytest.mark.asyncio
async def test_present_intent_dispatches_normally(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await ToolDispatcher.dispatch(
            dispatcher,
            "create_file",
            {
                "intent": "create the fixture file this test asserts on",
                "path": "made.txt",
                "content": "hi",
            },
        )
    )
    assert result["status"] == "created"
    assert (tmp_path / "made.txt").read_text(encoding="utf-8") == "hi"

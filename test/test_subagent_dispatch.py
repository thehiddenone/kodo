"""Behavior tests for kodo.runtime._subagent_dispatch.

Tests verify that SubagentDispatcher:
- Routes publish_artifact to the Workspace and tracks published IDs.
- Routes read_artifact to the Workspace and returns serialized results.
- Routes report_artifact_completed without forcing a stop.
- Routes escalate_blocker and sets stop_requested=True.
- Auto-accepts request_user_review_artifact in autonomous mode.
- Tools are correctly resolved for known agent tool names.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.runtime import (
    LEAF_TOOLS_BY_NAME,
    GateOrchestrator,
    QuestionResponse,
    SubagentDispatcher,
    tools_for_agent,
)
from kodo.toolspecs import PUBLISH_ARTIFACT, READ_ARTIFACT
from kodo.workspace import Artifact, ArtifactType, Workspace

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
    """Return a GateOrchestrator whose fire_question always resolves to answer."""
    state = _make_app_state()
    gate = GateOrchestrator(state, MagicMock())

    # Override fire_question to resolve immediately
    async def _instant_question(question: str, mode: str, choices=None) -> QuestionResponse:
        return QuestionResponse(answer_text=answer, choice_key="")

    gate.fire_question = _instant_question  # type: ignore[method-assign]
    return gate


def _make_workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def _make_dispatcher(
    tmp_path: Path,
    agent_name: str = "test_agent",
    answer: str = "",
    autonomous: bool = False,
    workspace: Workspace | None = None,
) -> SubagentDispatcher:
    return SubagentDispatcher(
        workspace=workspace if workspace is not None else _make_workspace(tmp_path),
        gate=_make_gate(answer),
        agent_name=agent_name,
        session_id="sess-test",
        autonomous=autonomous,
    )


def _make_artifact(
    artifact_id: str = "art-1",
    artifact_type: ArtifactType = ArtifactType.NARRATIVE,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        type=artifact_type,
        author="test_agent",
        project_code="TEST",
        responsibility_code="TEST",
        created_at=datetime.now(tz=UTC),
        content="content",
        filename_hint="out.md",
    )


# ---------------------------------------------------------------------------
# Tool spec constants
# ---------------------------------------------------------------------------


def test_publish_artifact_spec_has_correct_name() -> None:
    assert PUBLISH_ARTIFACT.name == "publish_artifact"


def test_read_artifact_spec_has_correct_name() -> None:
    assert READ_ARTIFACT.name == "read_artifact"


def test_leaf_tools_by_name_includes_workspace_and_report_tools() -> None:
    assert "publish_artifact" in LEAF_TOOLS_BY_NAME
    assert "read_artifact" in LEAF_TOOLS_BY_NAME
    assert "escalate_blocker" in LEAF_TOOLS_BY_NAME
    assert "ask_user" in LEAF_TOOLS_BY_NAME
    assert "request_user_review_artifact" in LEAF_TOOLS_BY_NAME
    assert "report_artifact_completed" in LEAF_TOOLS_BY_NAME


def test_leaf_tools_by_name_includes_fileio_and_shell_tools() -> None:
    assert "create_file" in LEAF_TOOLS_BY_NAME
    assert "edit_file" in LEAF_TOOLS_BY_NAME
    assert "delete_file" in LEAF_TOOLS_BY_NAME
    assert "copy_file" in LEAF_TOOLS_BY_NAME
    assert "move_file" in LEAF_TOOLS_BY_NAME
    assert "run_command" in LEAF_TOOLS_BY_NAME


# ---------------------------------------------------------------------------
# tools_for_agent
# ---------------------------------------------------------------------------


def test_tools_for_agent_returns_specs_for_declared_tools() -> None:
    agent = MagicMock()
    agent.tools = frozenset(["publish_artifact", "read_artifact"])
    result = tools_for_agent(agent)
    names = {t.name for t in result}
    assert "publish_artifact" in names
    assert "read_artifact" in names


def test_tools_for_agent_skips_unknown_tool_names() -> None:
    agent = MagicMock()
    agent.tools = frozenset(["publish_artifact", "nonexistent_tool"])
    result = tools_for_agent(agent)
    names = {t.name for t in result}
    assert "publish_artifact" in names
    assert "nonexistent_tool" not in names


# ---------------------------------------------------------------------------
# publish_artifact dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_artifact_returns_id(tmp_path: Path) -> None:
    """
    Given valid publish_artifact inputs,
    when dispatch is called,
    then the result contains an artifact ID and the ID is tracked.
    """
    dispatcher = _make_dispatcher(tmp_path)
    result_str = await dispatcher.dispatch(
        "publish_artifact",
        {
            "type": "narrative",
            "project_code": "TEST",
            "responsibility_code": "TEST",
            "content": "A narrative",
        },
    )
    result = json.loads(result_str)
    assert "id" in result
    assert isinstance(result["id"], str)
    assert result["id"] in dispatcher.published_ids


@pytest.mark.asyncio
async def test_publish_artifact_forces_author_from_dispatcher(tmp_path: Path) -> None:
    """
    Even if the LLM provides a different author, publish uses the dispatcher's agent_name.
    """
    workspace = Workspace(tmp_path)
    dispatcher = _make_dispatcher(tmp_path, agent_name="narrative_author", workspace=workspace)
    await dispatcher.dispatch(
        "publish_artifact",
        {
            "type": "narrative",
            "project_code": "TEST",
            "responsibility_code": "TEST",
            "content": "test",
            "author": "impersonated_agent",  # should be ignored
        },
    )
    # Read back via the same workspace (shared index).
    arts = await workspace.read(artifact_id=dispatcher.published_ids[0])
    assert arts[0].author == "narrative_author"


@pytest.mark.asyncio
async def test_publish_artifact_missing_required_field_returns_error(tmp_path: Path) -> None:
    """
    When required fields are missing,
    the result contains an error key.
    """
    dispatcher = _make_dispatcher(tmp_path)
    result_str = await dispatcher.dispatch(
        "publish_artifact",
        {"type": "narrative"},  # missing project_code, responsibility_code, content
    )
    result = json.loads(result_str)
    assert "error" in result


@pytest.mark.asyncio
async def test_publish_artifact_accumulates_multiple_ids(tmp_path: Path) -> None:
    """
    Two successive publishes both end up in published_ids.
    """
    dispatcher = _make_dispatcher(tmp_path)
    for _ in range(2):
        await dispatcher.dispatch(
            "publish_artifact",
            {
                "type": "narrative",
                "project_code": "TEST",
                "responsibility_code": "TEST",
                "content": "content",
            },
        )
    assert len(dispatcher.published_ids) == 2


# ---------------------------------------------------------------------------
# read_artifact dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_artifact_returns_list(tmp_path: Path) -> None:
    """
    When no artifacts match a filter,
    read_artifact returns an empty list (not an error).
    """
    dispatcher = _make_dispatcher(tmp_path)
    result_str = await dispatcher.dispatch(
        "read_artifact",
        {"artifact_id": "nonexistent-id"},
    )
    result = json.loads(result_str)
    assert isinstance(result, list)
    assert result == []


@pytest.mark.asyncio
async def test_read_artifact_returns_published_artifact(tmp_path: Path) -> None:
    """
    After publishing an artifact,
    read_artifact by artifact_id returns that artifact.
    """
    dispatcher = _make_dispatcher(tmp_path, agent_name="narrative_author")
    publish_result = json.loads(
        await dispatcher.dispatch(
            "publish_artifact",
            {
                "type": "narrative",
                "project_code": "PROJ",
                "responsibility_code": "PROJ",
                "content": "Hello narrative",
                "filename_hint": "narrative.md",
            },
        )
    )
    artifact_id = publish_result["id"]

    read_result = json.loads(
        await dispatcher.dispatch("read_artifact", {"artifact_id": artifact_id})
    )
    assert len(read_result) == 1
    assert read_result[0]["id"] == artifact_id
    assert read_result[0]["content"] == "Hello narrative"


# ---------------------------------------------------------------------------
# report_artifact_completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_artifact_completed_does_not_stop(tmp_path: Path) -> None:
    """
    report_artifact_completed is a per-artifact signal and does NOT force a
    stop — a solo agent may report several artifacts and then end naturally.
    """
    dispatcher = _make_dispatcher(tmp_path)
    assert not dispatcher.stop_requested

    result_str = await dispatcher.dispatch(
        "report_artifact_completed",
        {"artifact_id": "n1"},
    )
    result = json.loads(result_str)

    assert result["status"] == "completed"
    assert result["artifact_id"] == "n1"
    assert not dispatcher.stop_requested


# ---------------------------------------------------------------------------
# escalate_blocker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_blocker_sets_stop_flag(tmp_path: Path) -> None:
    """
    When escalate_blocker is dispatched (interactive), stop_requested becomes
    True and the user's reply is relayed back.
    """
    dispatcher = _make_dispatcher(tmp_path, answer="user reply")
    result_str = await dispatcher.dispatch(
        "escalate_blocker",
        {"reason": "cap_reached", "summary": "Need help"},
    )
    result = json.loads(result_str)
    assert result["status"] == "escalated"
    assert "user_response" in result
    assert dispatcher.stop_requested


# ---------------------------------------------------------------------------
# request_user_review_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_user_review_artifact_autonomous_auto_accepts(tmp_path: Path) -> None:
    """
    In autonomous mode, request_user_review_artifact returns an accept without
    blocking on the user, and does not request a stop.
    """
    dispatcher = _make_dispatcher(tmp_path, autonomous=True)
    result_str = await dispatcher.dispatch(
        "request_user_review_artifact",
        {"artifact_id": "a1"},
    )
    result = json.loads(result_str)
    assert result["action"] == "agree"
    assert not dispatcher.stop_requested


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
async def test_edit_file_replaces_content(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("old", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("edit_file", {"path": "out.txt", "content": "new"})
    )
    assert result["status"] == "edited"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_edit_file_fails_if_missing(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("edit_file", {"path": "missing.txt", "content": "new"})
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_delete_file_removes_file(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("content", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("delete_file", {"path": "out.txt"}))
    assert result["status"] == "deleted"
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_copy_file_copies_content(tmp_path: Path) -> None:
    (tmp_path / "src.txt").write_text("content", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("copy_file", {"source": "src.txt", "destination": "dst.txt"})
    )
    assert result["status"] == "copied"
    assert (tmp_path / "src.txt").read_text(encoding="utf-8") == "content"
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
async def test_move_file_renames_file(tmp_path: Path) -> None:
    (tmp_path / "src.txt").write_text("content", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("move_file", {"source": "src.txt", "destination": "dst.txt"})
    )
    assert result["status"] == "moved"
    assert not (tmp_path / "src.txt").exists()
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
async def test_fileio_rejects_path_outside_project_root(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("create_file", {"path": "../escape.txt", "content": "nope"})
    )
    assert "error" in result
    assert not (tmp_path.parent / "escape.txt").exists()


# ---------------------------------------------------------------------------
# Native shell tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_returns_exit_code_and_output(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("run_command", {"command": "echo hello"}))
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_run_command_rejects_working_dir_outside_project_root(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("run_command", {"command": "pwd", "working_dir": ".."})
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result_str = await dispatcher.dispatch("no_such_tool", {})
    result = json.loads(result_str)
    assert "error" in result

"""Behavior tests for the leaf-agent tools in :mod:`kodo.tools`.

Every agent shares one :class:`~kodo.tools.ToolDispatcher`; these tests
exercise the tools a leaf sub-agent typically holds — workspace I/O
(``publish_artifact``, ``read_artifact``), the report tools
(``report_artifact_completed``, ``escalate_blocker``,
``request_user_review_artifact``), and the native file-I/O / shell tools — plus
the shared ``tools_for_agent`` resolver.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.runtime import GateOrchestrator, QuestionResponse
from kodo.tools import DISPATCHABLE_TOOLS_BY_NAME, ToolDispatcher, tools_for_agent
from kodo.toolspecs import PUBLISH_ARTIFACT, READ_ARTIFACT
from kodo.workspace import Artifact, ArtifactType, ProjectIndex, Workspace

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
    gate = GateOrchestrator(_make_app_state(), MagicMock())

    async def _instant_question(
        question: str, mode: str, choices: list[dict[str, str]] | None = None
    ) -> QuestionResponse:
        return QuestionResponse(answer_text=answer, choice_key="")

    gate.fire_question = _instant_question  # type: ignore[method-assign]
    return gate


class _StubRunner:
    """Sub-agent launcher stub; leaf tools never invoke it."""

    async def run_subagent(
        self, name: str, task_message: str, input_artifact_ids: list[str]
    ) -> list[str]:
        return []

    async def run_author_critic_iteration(
        self,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        previous_artifact_id: str | None,
    ) -> dict[str, object]:
        return {"artifact_id": None, "verdict": "accepted", "concerns": []}


def _make_dispatcher(
    tmp_path: Path,
    agent_name: str = "test_agent",
    answer: str = "",
    autonomous: bool = False,
    workspace: Workspace | None = None,
) -> ToolDispatcher:
    index = ProjectIndex()
    ws = workspace if workspace is not None else Workspace(tmp_path, index)

    async def _noop_rollback(target_sha: str) -> None:
        return None

    return ToolDispatcher(
        workspace=ws,
        index=index,
        gate=_make_gate(answer),
        session=MagicMock(),
        runner=_StubRunner(),
        rollback_fn=_noop_rollback,
        complete_fn=ws.mark_completed,
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
# Tool spec constants & catalog
# ---------------------------------------------------------------------------


def test_publish_artifact_spec_has_correct_name() -> None:
    assert PUBLISH_ARTIFACT.name == "publish_artifact"


def test_read_artifact_spec_has_correct_name() -> None:
    assert READ_ARTIFACT.name == "read_artifact"


def test_dispatchable_catalog_includes_workspace_and_report_tools() -> None:
    for name in (
        "publish_artifact",
        "read_artifact",
        "escalate_blocker",
        "ask_user",
        "request_user_review_artifact",
        "report_artifact_completed",
    ):
        assert name in DISPATCHABLE_TOOLS_BY_NAME


def test_dispatchable_catalog_includes_fileio_and_shell_tools() -> None:
    for name in (
        "create_file",
        "edit_file",
        "delete_file",
        "copy_file",
        "move_file",
        "run_command",
    ):
        assert name in DISPATCHABLE_TOOLS_BY_NAME


# ---------------------------------------------------------------------------
# tools_for_agent (takes tool names, not a SubAgent)
# ---------------------------------------------------------------------------


def test_tools_for_agent_returns_specs_for_declared_tools() -> None:
    result = tools_for_agent(frozenset(["publish_artifact", "read_artifact"]))
    names = {t.name for t in result}
    assert names == {"publish_artifact", "read_artifact"}


def test_tools_for_agent_skips_unknown_tool_names() -> None:
    result = tools_for_agent(frozenset(["publish_artifact", "nonexistent_tool"]))
    names = {t.name for t in result}
    assert "publish_artifact" in names
    assert "nonexistent_tool" not in names


# ---------------------------------------------------------------------------
# publish_artifact dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_artifact_returns_id(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "publish_artifact",
            {
                "type": "narrative",
                "project_code": "TEST",
                "responsibility_code": "TEST",
                "content": "A narrative",
            },
        )
    )
    assert isinstance(result["id"], str)
    assert result["id"] in dispatcher.published_ids


@pytest.mark.asyncio
async def test_publish_artifact_forces_author_from_dispatcher(tmp_path: Path) -> None:
    """Even if the LLM provides a different author, publish uses agent_name."""
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
    arts = await workspace.read(artifact_id=dispatcher.published_ids[0])
    assert arts[0].author == "narrative_author"


@pytest.mark.asyncio
async def test_publish_artifact_missing_required_field_returns_error(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(await dispatcher.dispatch("publish_artifact", {"type": "narrative"}))
    assert "error" in result


@pytest.mark.asyncio
async def test_publish_artifact_accumulates_multiple_ids(tmp_path: Path) -> None:
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
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("read_artifact", {"artifact_id": "nonexistent-id"})
    )
    assert result == []


@pytest.mark.asyncio
async def test_read_artifact_returns_published_artifact(tmp_path: Path) -> None:
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
    dispatcher = _make_dispatcher(tmp_path)
    assert not dispatcher.stop_requested

    result = json.loads(
        await dispatcher.dispatch("report_artifact_completed", {"artifact_id": "n1"})
    )

    assert result["status"] == "completed"
    assert result["artifact_id"] == "n1"
    assert not dispatcher.stop_requested


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
# request_user_review_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_user_review_artifact_autonomous_auto_accepts(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, autonomous=True)
    result = json.loads(
        await dispatcher.dispatch("request_user_review_artifact", {"artifact_id": "a1"})
    )
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
    result = json.loads(await dispatcher.dispatch("no_such_tool", {}))
    assert "error" in result

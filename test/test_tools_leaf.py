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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.runtime import GateOrchestrator, QuestionResponse, SessionState
from kodo.tools import (
    DISPATCHABLE_TOOLS_BY_NAME,
    ProjectPathResolver,
    ToolDispatcher,
    tools_for_agent,
)
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


class _StubServices:
    """Engine-side stub satisfying ``kodo.tools.EngineServices``.

    The sub-agent launchers are never invoked by leaf tools; ``complete_artifact``
    delegates to the injected callback (the workspace's ``mark_completed``).
    """

    def __init__(self, complete_artifact: Callable[[str], Awaitable[None]]) -> None:
        self._complete = complete_artifact

    async def run_subagent(
        self, caller: str, name: str, task_message: str, input_artifact_ids: list[str]
    ) -> list[str]:
        return []

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        previous_artifact_id: str | None,
    ) -> dict[str, object]:
        return {"artifact_id": None, "verdict": "accepted", "concerns": []}

    async def rollback(self, target_sha: str) -> None:
        return None

    async def complete_artifact(self, artifact_id: str) -> None:
        await self._complete(artifact_id)

    async def disable_autonomous_mode(self) -> None:
        return None

    async def post_update(self, message: str) -> None:
        return None


def _make_dispatcher(
    tmp_path: Path,
    agent_name: str = "test_agent",
    answer: str = "",
    autonomous: bool = False,
    workspace: Workspace | None = None,
) -> ToolDispatcher:
    index = ProjectIndex()
    ws = workspace if workspace is not None else Workspace(tmp_path, index)
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous

    return ToolDispatcher(
        workspace=ws,
        index=index,
        resolver=ProjectPathResolver(tmp_path),
        gate=_make_gate(answer),
        session=session,
        services=_StubServices(complete_artifact=ws.mark_completed),
        agent_name=agent_name,
        session_id="sess-test",
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
        "filesystem",
        "edit_file",
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
    assert result == {"artifacts": []}


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
    artifacts = read_result["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["id"] == artifact_id
    assert artifacts[0]["content"] == "Hello narrative"


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
        await dispatcher.dispatch(
            "filesystem", {"operation": "create_file", "path": "out.txt", "content": "hello"}
        )
    )
    assert result["status"] == "created"
    assert result["operation"] == "create_file"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_create_file_fails_if_already_exists(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("existing", encoding="utf-8")
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem", {"operation": "create_file", "path": "out.txt", "content": "hello"}
        )
    )
    assert "error" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "existing"


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
async def test_create_dir_makes_parents(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch("filesystem", {"operation": "create_dir", "path": "a/b/c"})
    )
    assert result["status"] == "created"
    assert (tmp_path / "a" / "b" / "c").is_dir()


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
async def test_fileio_rejects_path_outside_project_root(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "filesystem", {"operation": "create_file", "path": "../escape.txt", "content": "nope"}
        )
    )
    assert "error" in result
    assert not (tmp_path.parent / "escape.txt").exists()


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
async def test_run_command_rejects_working_dir_outside_project_root(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result = json.loads(
        await dispatcher.dispatch(
            "run_command", {"command": "pwd", "working_dir": "..", "timeout": 10}
        )
    )
    assert "error" in result


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

"""Behavior tests for kodo.runtime._subagent_dispatch.

Tests verify that SubagentDispatcher:
- Routes publish_artifact to the Workspace and tracks published IDs.
- Routes read_artifact to the Workspace and returns serialized results.
- Routes narrative_report_completed and sets stop_requested=True.
- Routes escalate_to_user and sets stop_requested=True.
- Tools are correctly resolved for known agent tool names.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.runtime._gates import GateOrchestrator, QuestionResponse
from kodo.runtime._subagent_dispatch import (
    LEAF_TOOLS_BY_NAME,
    PUBLISH_ARTIFACT_SPEC,
    READ_ARTIFACT_SPEC,
    SubagentDispatcher,
    tools_for_agent,
)
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
) -> SubagentDispatcher:
    return SubagentDispatcher(
        workspace=_make_workspace(tmp_path),
        gate=_make_gate(answer),
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
# Tool spec constants
# ---------------------------------------------------------------------------


def test_publish_artifact_spec_has_correct_name() -> None:
    assert PUBLISH_ARTIFACT_SPEC.name == "publish_artifact"


def test_read_artifact_spec_has_correct_name() -> None:
    assert READ_ARTIFACT_SPEC.name == "read_artifact"


def test_leaf_tools_by_name_includes_workspace_and_report_tools() -> None:
    assert "publish_artifact" in LEAF_TOOLS_BY_NAME
    assert "read_artifact" in LEAF_TOOLS_BY_NAME
    assert "escalate_to_user" in LEAF_TOOLS_BY_NAME
    assert "narrative_ask_user_question" in LEAF_TOOLS_BY_NAME
    assert "narrative_present_for_acceptance" in LEAF_TOOLS_BY_NAME
    assert "narrative_report_completed" in LEAF_TOOLS_BY_NAME


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
    dispatcher = _make_dispatcher(tmp_path, agent_name="narrative_author")
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
    # Read the artifact back to check author
    workspace = Workspace(tmp_path)
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
# narrative_report_completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrative_report_completed_sets_stop_flag(tmp_path: Path) -> None:
    """
    When narrative_report_completed is dispatched,
    stop_requested becomes True.
    """
    dispatcher = _make_dispatcher(tmp_path)
    assert not dispatcher.stop_requested

    result_str = await dispatcher.dispatch(
        "narrative_report_completed",
        {"narrative_artifact_id": "n1", "tech_stack_artifact_id": "ts1"},
    )
    result = json.loads(result_str)

    assert result["status"] == "completed"
    assert dispatcher.stop_requested


# ---------------------------------------------------------------------------
# escalate_to_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_to_user_sets_stop_flag(tmp_path: Path) -> None:
    """
    When escalate_to_user is dispatched,
    stop_requested becomes True.
    """
    dispatcher = _make_dispatcher(tmp_path, answer="user reply")
    result_str = await dispatcher.dispatch(
        "escalate_to_user",
        {"reason": "cap_reached", "summary": "Need help"},
    )
    result = json.loads(result_str)
    assert "user_response" in result
    assert dispatcher.stop_requested


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    result_str = await dispatcher.dispatch("no_such_tool", {})
    result = json.loads(result_str)
    assert "error" in result

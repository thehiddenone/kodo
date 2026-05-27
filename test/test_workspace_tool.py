"""Behavior tests for kodo.tools.workspace._server.WorkspaceTool.

WorkspaceTool wraps a Workspace via MCP tools.  Tests cover the constructor
and end-to-end publish/read round-trips by calling the async tool bound
functions directly (via Tool.fn on the FastMCP ToolManager).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.tools.workspace._server import WorkspaceTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app(instance: WorkspaceTool) -> object:
    return vars(instance)["_WorkspaceTool__app"]


def _tool_fn(instance: WorkspaceTool, tool_name: str) -> object:
    app = _app(instance)
    return app._tool_manager._tools[tool_name].fn


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_workspace_tool_can_be_created(tmp_path: Path) -> None:
    """
    Given a valid project_root path,
    when WorkspaceTool is instantiated,
    then no exception is raised.
    """
    WorkspaceTool(project_root=tmp_path)


def test_workspace_tool_accepts_string_path(tmp_path: Path) -> None:
    """
    Given a string project_root path,
    when WorkspaceTool is instantiated,
    then no exception is raised.
    """
    WorkspaceTool(project_root=str(tmp_path))


def test_workspace_tool_registers_publish_artifact_tool(tmp_path: Path) -> None:
    """
    Given a WorkspaceTool instance,
    when tools are listed,
    then 'publish_artifact' is registered.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    app = _app(wt)
    assert "publish_artifact" in app._tool_manager._tools


def test_workspace_tool_registers_read_artifact_tool(tmp_path: Path) -> None:
    """
    Given a WorkspaceTool instance,
    when tools are listed,
    then 'read_artifact' is registered.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    app = _app(wt)
    assert "read_artifact" in app._tool_manager._tools


def test_workspace_tool_registers_exactly_two_tools(tmp_path: Path) -> None:
    """
    Given a WorkspaceTool instance,
    when tools are counted,
    then exactly two tools are registered.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    app = _app(wt)
    assert len(app._tool_manager._tools) == 2


# ---------------------------------------------------------------------------
# publish_artifact / read_artifact round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_and_read_artifact_round_trip(tmp_path: Path) -> None:
    """
    Given a WorkspaceTool,
    when publish_artifact is called and then read_artifact with the returned id,
    then the read result contains the published content.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    publish_fn = _tool_fn(wt, "publish_artifact")
    read_fn = _tool_fn(wt, "read_artifact")

    artifact_id = await publish_fn(
        type="narrative",
        author="test_agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="The project builds a trading bot.",
    )

    assert isinstance(artifact_id, str)
    assert len(artifact_id) > 0

    results = await read_fn(artifact_id=artifact_id)
    assert len(results) == 1
    assert results[0]["content"] == "The project builds a trading bot."


@pytest.mark.asyncio
async def test_publish_artifact_returns_unique_ids(tmp_path: Path) -> None:
    """
    Given two publish_artifact calls,
    when each returns an ID,
    then the IDs are distinct.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    publish_fn = _tool_fn(wt, "publish_artifact")

    id1 = await publish_fn(
        type="narrative",
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="First artifact",
    )
    id2 = await publish_fn(
        type="requirements",
        author="agent",
        project_code="PROJ",
        responsibility_code="COMP",
        content="Second artifact",
    )

    assert id1 != id2


@pytest.mark.asyncio
async def test_read_artifact_with_type_filter(tmp_path: Path) -> None:
    """
    Given two published artifacts of different types,
    when read_artifact is filtered by type,
    then only matching artifacts are returned.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    publish_fn = _tool_fn(wt, "publish_artifact")
    read_fn = _tool_fn(wt, "read_artifact")

    await publish_fn(
        type="narrative",
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Narrative content",
    )
    await publish_fn(
        type="requirements",
        author="agent",
        project_code="PROJ",
        responsibility_code="COMP",
        content="Requirements content",
    )

    results = await read_fn(type="narrative", version="in_flight")
    assert all(r["type"] == "narrative" for r in results)


@pytest.mark.asyncio
async def test_publish_artifact_with_concerns(tmp_path: Path) -> None:
    """
    Given a feedback artifact with concerns,
    when publish_artifact is called with the concerns list,
    then the published artifact can be read back with concerns data.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    publish_fn = _tool_fn(wt, "publish_artifact")
    read_fn = _tool_fn(wt, "read_artifact")

    # First publish the artifact under review
    reviewed_id = await publish_fn(
        type="narrative",
        author="author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="A narrative",
    )

    feedback_id = await publish_fn(
        type="feedback",
        author="critic",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Feedback content",
        reviewed_artifact_id=reviewed_id,
        verdict="rejected",
        concerns=[{"kind": "completeness", "description": "Missing details"}],
    )

    results = await read_fn(artifact_id=feedback_id)
    assert len(results) == 1
    assert len(results[0]["concerns"]) == 1
    assert results[0]["concerns"][0]["kind"] == "completeness"


@pytest.mark.asyncio
async def test_read_artifact_exclude_content(tmp_path: Path) -> None:
    """
    Given a published artifact,
    when read_artifact is called with include_content=False,
    then the returned dict has an empty content field.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    publish_fn = _tool_fn(wt, "publish_artifact")
    read_fn = _tool_fn(wt, "read_artifact")

    artifact_id = await publish_fn(
        type="narrative",
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Secret content",
    )

    results = await read_fn(artifact_id=artifact_id, include_content=False)
    assert len(results) == 1
    assert results[0]["content"] is None


@pytest.mark.asyncio
async def test_publish_artifact_with_session_id(tmp_path: Path) -> None:
    """
    Given a publish_artifact call with a session_id,
    when the artifact is read back,
    then the session_id is preserved.
    """
    wt = WorkspaceTool(project_root=tmp_path)
    publish_fn = _tool_fn(wt, "publish_artifact")
    read_fn = _tool_fn(wt, "read_artifact")

    artifact_id = await publish_fn(
        type="narrative",
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Content with session",
        session_id="session-xyz",
    )

    results = await read_fn(artifact_id=artifact_id)
    assert results[0]["session_id"] == "session-xyz"

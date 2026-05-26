"""Behavioral tests for kodo.workspace.

Tests verify observable side-effects (artifact round-trip, session_id
persistence, validation rules) without inspecting internal implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.workspace import ArtifactType, Workspace
from kodo.workspace._errors import ArtifactNotFoundError, WorkspaceValidationError


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


# ---------------------------------------------------------------------------
# session_id: stored and round-tripped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_id_is_persisted_on_publish(tmp_path: Path) -> None:
    """
    Given a workspace,
    when an artifact is published with a session_id,
    then reading it back returns the same session_id.
    """
    ws = _workspace(tmp_path)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="narrative_author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="# Narrative",
        session_id="session-abc-123",
    )

    results = await ws.read(artifact_id=artifact_id)
    assert len(results) == 1
    assert results[0].session_id == "session-abc-123"


@pytest.mark.asyncio
async def test_session_id_is_none_when_not_supplied(tmp_path: Path) -> None:
    """
    Given a workspace,
    when an artifact is published without a session_id,
    then reading it back returns session_id=None.
    """
    ws = _workspace(tmp_path)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.REQUIREMENTS,
        author="requirements_author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="REQ-001: something",
    )

    results = await ws.read(artifact_id=artifact_id)
    assert results[0].session_id is None


@pytest.mark.asyncio
async def test_session_id_survives_index_rebuild(tmp_path: Path) -> None:
    """
    Given a workspace where an artifact was published with a session_id,
    when the workspace is rebuilt from its event log (simulating cold start),
    then the session_id is still present on the artifact.
    """
    ws = _workspace(tmp_path)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.ARCHITECTURE,
        author="architect",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="# Architecture",
        session_id="session-rebuild-test",
    )

    ws2 = _workspace(tmp_path)
    await ws2.rebuild_index()
    results = await ws2.read(artifact_id=artifact_id)
    assert results[0].session_id == "session-rebuild-test"


@pytest.mark.asyncio
async def test_session_id_on_superseding_artifact(tmp_path: Path) -> None:
    """
    Given an artifact with session_id A is published,
    when it is superseded by a new artifact with session_id B,
    then only the new artifact (session B) is live; retired artifact is gone.
    """
    ws = _workspace(tmp_path)
    first_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="narrative_author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="v1",
        session_id="session-A",
    )
    second_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="narrative_author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="v2",
        supersedes=[first_id],
        session_id="session-B",
    )

    live = await ws.read(
        project_code="PROJ",
        responsibility_code="PROJ",
        artifact_type=ArtifactType.NARRATIVE,
        version="in_flight",
    )
    assert len(live) == 1
    assert live[0].id == second_id
    assert live[0].session_id == "session-B"


# ---------------------------------------------------------------------------
# Validation: existing rules still hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_requires_at_least_one_filter_on_read(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.read()


@pytest.mark.asyncio
async def test_feedback_requires_reviewed_artifact_id(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.FEEDBACK,
            author="critic",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="feedback",
            verdict="accepted",
        )


@pytest.mark.asyncio
async def test_supersedes_nonexistent_artifact_raises(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        await ws.publish(
            artifact_type=ArtifactType.NARRATIVE,
            author="author",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="v2",
            supersedes=["nonexistent-id"],
        )


# ---------------------------------------------------------------------------
# version parameter: enforcement rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_read_without_version_raises(tmp_path: Path) -> None:
    """
    Given a filter-form read() call (no artifact_id),
    when version is omitted,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="content",
    )
    with pytest.raises(WorkspaceValidationError, match="version is required"):
        await ws.read(project_code="PROJ", artifact_type=ArtifactType.NARRATIVE)


@pytest.mark.asyncio
async def test_artifact_id_read_with_version_raises(tmp_path: Path) -> None:
    """
    Given an artifact_id-form read() call,
    when version is also supplied,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="content",
    )
    with pytest.raises(WorkspaceValidationError, match="version must not be specified"):
        await ws.read(artifact_id=artifact_id, version="in_flight")


@pytest.mark.asyncio
async def test_filter_read_with_version_in_flight_returns_results(tmp_path: Path) -> None:
    """
    Given an artifact in the workspace,
    when read() is called with version='in_flight' and matching filters,
    then the artifact is returned.
    """
    ws = _workspace(tmp_path)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="content",
    )
    results = await ws.read(
        project_code="PROJ",
        artifact_type=ArtifactType.NARRATIVE,
        version="in_flight",
    )
    assert any(a.id == artifact_id for a in results)

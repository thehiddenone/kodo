"""Behavioral tests for kodo.workspace.

Tests verify observable side-effects (artifact round-trip, session_id
persistence, validation rules) without inspecting internal implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.workspace import (
    ArtifactNotFoundError,
    ArtifactType,
    ProjectIndex,
    Workspace,
    WorkspaceValidationError,
)


def _workspace(tmp_path: Path, index: ProjectIndex | None = None) -> Workspace:
    return Workspace(tmp_path, index)


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
async def test_session_id_persisted_and_readable(tmp_path: Path) -> None:
    """
    Given an artifact published with a session_id,
    when a second Workspace over the same shared index reads it,
    then the session_id is still present on the artifact.
    """
    index = ProjectIndex()
    ws = _workspace(tmp_path, index)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.ARCHITECTURE,
        author="architect",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="# Architecture",
        session_id="session-rebuild-test",
    )

    ws2 = _workspace(tmp_path, index)
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


# ---------------------------------------------------------------------------
# project_root property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_root_property_returns_path(tmp_path: Path) -> None:
    """
    Given a Workspace,
    when project_root is accessed,
    then it returns the resolved project root path.
    """
    ws = _workspace(tmp_path)
    assert ws.project_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# read() filter: author
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_by_author_returns_only_matching_artifacts(tmp_path: Path) -> None:
    """
    Given two artifacts published by different authors,
    when read() is called with author='agent_a',
    then only agent_a's artifact is returned.
    """
    ws = _workspace(tmp_path)
    id_a = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent_a",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="by agent_a",
    )
    await ws.publish(
        artifact_type=ArtifactType.REQUIREMENTS,
        author="agent_b",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="by agent_b",
    )

    results = await ws.read(author="agent_a", version="in_flight")
    assert len(results) == 1
    assert results[0].id == id_a


# ---------------------------------------------------------------------------
# read() filter: requirement_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_by_requirement_id_returns_matching_artifacts(tmp_path: Path) -> None:
    """
    Given an artifact published with requirement_ids=['PROJ_COMP_REQ01'],
    when read() is called with requirement_id='PROJ_COMP_REQ01',
    then that artifact is returned.
    """
    ws = _workspace(tmp_path)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.REQUIREMENTS,
        author="agent",
        project_code="PROJ",
        responsibility_code="COMP",
        content="FR-01: something",
        requirement_ids=["PROJ_COMP_REQ01"],
    )
    # Also publish one without that requirement ID
    await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="No requirements",
    )

    results = await ws.read(requirement_id="PROJ_COMP_REQ01", version="in_flight")
    assert len(results) == 1
    assert results[0].id == artifact_id


# ---------------------------------------------------------------------------
# read() filter: verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_by_verdict_returns_accepted_feedback(tmp_path: Path) -> None:
    """
    Given a feedback artifact with verdict=accepted,
    when read() is called with verdict=Verdict.ACCEPTED,
    then the feedback artifact is returned.
    """
    from kodo.workspace import Verdict

    ws = _workspace(tmp_path)
    reviewed_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Narrative",
    )
    feedback_id = await ws.publish(
        artifact_type=ArtifactType.FEEDBACK,
        author="critic",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="LGTM",
        reviewed_artifact_id=reviewed_id,
        verdict=Verdict.ACCEPTED,
    )

    results = await ws.read(verdict=Verdict.ACCEPTED, version="in_flight")
    assert any(a.id == feedback_id for a in results)


# ---------------------------------------------------------------------------
# read() filter: concern_kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_by_concern_kind_returns_matching_feedback(tmp_path: Path) -> None:
    """
    Given a feedback artifact with a concern of kind 'completeness',
    when read() is called with concern_kind='completeness',
    then the feedback artifact is returned.
    """
    from kodo.workspace import Concern, Verdict

    ws = _workspace(tmp_path)
    reviewed_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Narrative",
    )
    feedback_id = await ws.publish(
        artifact_type=ArtifactType.FEEDBACK,
        author="critic",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Missing sections.",
        reviewed_artifact_id=reviewed_id,
        verdict=Verdict.REJECTED,
        concerns=[Concern(kind="completeness", description="Missing sections.")],
    )

    results = await ws.read(concern_kind="completeness", version="in_flight")
    assert any(a.id == feedback_id for a in results)


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_with_empty_author_raises(tmp_path: Path) -> None:
    """
    Given an empty author string,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.NARRATIVE,
            author="",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="content",
        )


@pytest.mark.asyncio
async def test_publish_with_invalid_project_code_raises(tmp_path: Path) -> None:
    """
    Given a project_code that doesn't match the expected format,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.NARRATIVE,
            author="agent",
            project_code="lowercase",
            responsibility_code="PROJ",
            content="content",
        )


@pytest.mark.asyncio
async def test_publish_with_invalid_responsibility_code_raises(tmp_path: Path) -> None:
    """
    Given a responsibility_code that doesn't match the expected format,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.REQUIREMENTS,
            author="agent",
            project_code="PROJ",
            responsibility_code="invalid-code",
            content="content",
        )


@pytest.mark.asyncio
async def test_publish_with_invalid_requirement_id_raises(tmp_path: Path) -> None:
    """
    Given a requirement_id that doesn't match the expected format,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.REQUIREMENTS,
            author="agent",
            project_code="PROJ",
            responsibility_code="COMP",
            content="content",
            requirement_ids=["bad-id"],
        )


@pytest.mark.asyncio
async def test_publish_feedback_without_verdict_raises(tmp_path: Path) -> None:
    """
    Given a feedback artifact with no verdict,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    reviewed_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Narrative",
    )
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.FEEDBACK,
            author="critic",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="feedback",
            reviewed_artifact_id=reviewed_id,
        )


@pytest.mark.asyncio
async def test_publish_feedback_rejected_without_concerns_raises(tmp_path: Path) -> None:
    """
    Given a feedback artifact with verdict=rejected but no concerns,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    from kodo.workspace import Verdict

    ws = _workspace(tmp_path)
    reviewed_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="Narrative",
    )
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.FEEDBACK,
            author="critic",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="feedback",
            reviewed_artifact_id=reviewed_id,
            verdict=Verdict.REJECTED,
        )


@pytest.mark.asyncio
async def test_publish_feedback_with_nonlive_reviewed_id_raises(tmp_path: Path) -> None:
    """
    Given a reviewed_artifact_id that is not a live artifact,
    when publish() is called with feedback type,
    then ArtifactNotFoundError is raised.
    """
    from kodo.workspace import Verdict

    ws = _workspace(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        await ws.publish(
            artifact_type=ArtifactType.FEEDBACK,
            author="critic",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="feedback",
            reviewed_artifact_id="nonexistent-artifact-id",
            verdict=Verdict.ACCEPTED,
        )


@pytest.mark.asyncio
async def test_publish_non_feedback_with_reviewed_artifact_id_raises(tmp_path: Path) -> None:
    """
    Given a non-feedback artifact with reviewed_artifact_id set,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.NARRATIVE,
            author="agent",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="content",
            reviewed_artifact_id="some-id",
        )


@pytest.mark.asyncio
async def test_publish_non_feedback_with_verdict_raises(tmp_path: Path) -> None:
    """
    Given a non-feedback artifact with verdict set,
    when publish() is called,
    then WorkspaceValidationError is raised.
    """
    from kodo.workspace import Verdict

    ws = _workspace(tmp_path)
    with pytest.raises(WorkspaceValidationError):
        await ws.publish(
            artifact_type=ArtifactType.NARRATIVE,
            author="agent",
            project_code="PROJ",
            responsibility_code="PROJ",
            content="content",
            verdict=Verdict.ACCEPTED,
        )


# ---------------------------------------------------------------------------
# Second workspace over the shared index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_workspace_reads_existing_artifacts(tmp_path: Path) -> None:
    """
    Given an artifact published via workspace1,
    when a second Workspace over the same shared index reads it,
    then the artifact is found.
    """
    index = ProjectIndex()
    ws1 = _workspace(tmp_path, index)
    artifact_id = await ws1.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="content",
    )

    ws2 = _workspace(tmp_path, index)
    results = await ws2.read(artifact_id=artifact_id)
    assert len(results) == 1
    assert results[0].id == artifact_id


# ---------------------------------------------------------------------------
# Superseding removes the prior artifact from the index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superseded_artifact_removed_from_index(tmp_path: Path) -> None:
    """
    Given artifact A that is superseded by artifact B,
    when in-flight artifacts are read,
    then only B is live (A was retired and removed from the index).
    """
    ws = _workspace(tmp_path)
    id_a = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="v1",
    )
    id_b = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="v2",
        supersedes=[id_a],
    )

    results = await ws.read(
        project_code="PROJ",
        artifact_type=ArtifactType.NARRATIVE,
        version="in_flight",
    )
    ids = {a.id for a in results}
    assert id_b in ids
    assert id_a not in ids


# ---------------------------------------------------------------------------
# Completion flips the index entry to completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_completed_moves_entry_to_completed(tmp_path: Path) -> None:
    """
    Given a published (in-flight) artifact,
    when mark_completed is called,
    then the index reports it completed and version='stable' reads return it
    while version='in_flight' does not.
    """
    index = ProjectIndex()
    ws = _workspace(tmp_path, index)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="content",
    )
    assert {e.artifact_id for e in index.in_flight_entries()} == {artifact_id}

    await ws.mark_completed(artifact_id)

    assert {e.artifact_id for e in index.completed_entries()} == {artifact_id}
    assert index.in_flight_entries() == []

    stable = await ws.read(
        project_code="PROJ", artifact_type=ArtifactType.NARRATIVE, version="stable"
    )
    assert artifact_id in {a.id for a in stable}
    in_flight = await ws.read(
        project_code="PROJ", artifact_type=ArtifactType.NARRATIVE, version="in_flight"
    )
    assert artifact_id not in {a.id for a in in_flight}


@pytest.mark.asyncio
async def test_mark_completed_with_location_removes_staging_file(tmp_path: Path) -> None:
    """
    Given a published artifact with a staging file,
    when mark_completed is called with a promoted location,
    then the staging file is deleted and the entry records the new location.
    """
    index = ProjectIndex()
    ws = _workspace(tmp_path, index)
    artifact_id = await ws.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="content",
    )
    before = index.get_by_id(artifact_id)
    assert before is not None
    staging_path = before.location
    assert staging_path.exists()

    promoted = tmp_path / "specs" / "narrative" / "narrative.md"
    await ws.mark_completed(artifact_id, location=promoted)

    assert not staging_path.exists()
    entry = index.get_by_id(artifact_id)
    assert entry is not None
    assert entry.state == "completed"
    assert entry.location == promoted

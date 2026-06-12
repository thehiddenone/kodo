"""Behavior tests for kodo.runtime._tool_surface.

Tests verify that ToolSurface handlers produce correct JSON responses for the
two read-only tools (compute_frontier, list_artifacts) and the terminal tool
(finalize_project).  Approval and ask_user are tested via the session.autonomous
fast-path.  Stub tools (start_subagent, run_author_critic_iteration) are
tested to confirm they return the expected shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.runtime._gates import GateOrchestrator
from kodo.runtime._index import IndexEntry, ProjectIndex
from kodo.runtime._session import SessionState
from kodo.runtime._tool_surface import ToolSurface
from kodo.workspace._models import ArtifactType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    artifact_id: str,
    responsibility_code: str,
    artifact_type: ArtifactType,
    state: str = "completed",
    author: str = "test_agent",
) -> IndexEntry:
    return IndexEntry(
        artifact_id=artifact_id,
        project_code="TEST",
        responsibility_code=responsibility_code,
        type=artifact_type,
        state=state,  # type: ignore[arg-type]
        location=Path(f"/tmp/{artifact_id}"),
        filename_hint=f"{artifact_type.value}.md",
        supersedes=[],
        requirement_ids=[],
        session_id=None,
        author=author,
        last_modified=datetime.now(tz=UTC),
    )


def _make_app_state() -> MagicMock:
    state = MagicMock()
    state.send = AsyncMock()
    return state


def _make_surface(
    index: ProjectIndex | None = None,
    autonomous: bool = False,
) -> ToolSurface:
    if index is None:
        index = ProjectIndex()
    session = SessionState()
    session.autonomous = autonomous
    gate = GateOrchestrator(_make_app_state(), MagicMock())

    async def _stub_run_subagent(name: str, task: str, ids: list[str]) -> list[str]:
        return [f"stub-artifact-{name}"]

    async def _stub_run_author_critic(
        author: str, critic: str, ids: list[str], prev: str | None
    ) -> dict[str, object]:
        return {"artifact_id": f"stub-{author}", "verdict": "accepted", "concerns": []}

    async def _stub_rollback(target_sha: str) -> None:
        pass

    return ToolSurface(
        index=index,
        gate=gate,
        session=session,
        run_subagent_fn=_stub_run_subagent,
        run_author_critic_fn=_stub_run_author_critic,
        rollback_fn=_stub_rollback,
    )


# ---------------------------------------------------------------------------
# compute_frontier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_frontier_empty_index_returns_empty_list() -> None:
    """
    Given an empty index,
    when compute_frontier is called,
    then the result has an empty frontier.
    """
    surface = _make_surface()
    result = json.loads(await surface.dispatch("compute_frontier", {}))
    assert result == {"frontier": []}


@pytest.mark.asyncio
async def test_compute_frontier_reports_first_missing_type() -> None:
    """
    Given a responsibility with a completed functional-design but no test-plan,
    when compute_frontier is called,
    then the frontier shows test-plan as the next type.
    """
    index = ProjectIndex()
    index.add(_make_entry("a1", "AUTH", ArtifactType.FUNCTIONAL_DESIGN))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("compute_frontier", {}))

    assert len(result["frontier"]) == 1
    entry = result["frontier"][0]
    assert entry["responsibility_code"] == "AUTH"
    assert entry["next_type"] == "test-plan"


@pytest.mark.asyncio
async def test_compute_frontier_skips_fully_complete_responsibilities() -> None:
    """
    Given a responsibility with all four execution types completed,
    when compute_frontier is called,
    then that responsibility does not appear in the frontier.
    """
    index = ProjectIndex()
    for artifact_type in (
        ArtifactType.FUNCTIONAL_DESIGN,
        ArtifactType.TEST_PLAN,
        ArtifactType.TEST,
        ArtifactType.CODE,
    ):
        index.add(_make_entry(f"x-{artifact_type.value}", "TRADE", artifact_type))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("compute_frontier", {}))
    codes = [e["responsibility_code"] for e in result["frontier"]]
    assert "TRADE" not in codes


@pytest.mark.asyncio
async def test_compute_frontier_multiple_responsibilities() -> None:
    """
    Given two responsibilities at different stages,
    when compute_frontier is called,
    then each appears with its correct next type.
    """
    index = ProjectIndex()
    index.add(_make_entry("a1", "AUTH", ArtifactType.FUNCTIONAL_DESIGN))
    index.add(_make_entry("a2", "AUTH", ArtifactType.TEST_PLAN))
    index.add(_make_entry("b1", "TRADE", ArtifactType.FUNCTIONAL_DESIGN))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("compute_frontier", {}))

    by_code = {e["responsibility_code"]: e["next_type"] for e in result["frontier"]}
    assert by_code["AUTH"] == "test"
    assert by_code["TRADE"] == "test-plan"


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_artifacts_requires_at_least_one_filter() -> None:
    """
    Given no filters,
    when list_artifacts is called,
    then the result is an error.
    """
    surface = _make_surface()
    result = json.loads(await surface.dispatch("list_artifacts", {}))
    assert "error" in result


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_type() -> None:
    """
    Given entries of different types in the index,
    when list_artifacts is called with type='code',
    then only code entries are returned.
    """
    index = ProjectIndex()
    index.add(_make_entry("c1", "AUTH", ArtifactType.CODE))
    index.add(_make_entry("t1", "AUTH", ArtifactType.TEST))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("list_artifacts", {"type": "code"}))
    ids = [a["artifact_id"] for a in result["artifacts"]]
    assert ids == ["c1"]


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_responsibility_code() -> None:
    """
    Given entries for two responsibility codes,
    when list_artifacts is called with responsibility_code='AUTH',
    then only AUTH entries are returned.
    """
    index = ProjectIndex()
    index.add(_make_entry("a1", "AUTH", ArtifactType.CODE))
    index.add(_make_entry("b1", "TRADE", ArtifactType.CODE))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("list_artifacts", {"responsibility_code": "AUTH"}))
    ids = [a["artifact_id"] for a in result["artifacts"]]
    assert ids == ["a1"]


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_state() -> None:
    """
    Given completed and in-flight entries,
    when list_artifacts is called with state='in_flight',
    then only in-flight entries are returned.
    """
    index = ProjectIndex()
    index.add(_make_entry("done", "AUTH", ArtifactType.CODE, state="completed"))
    index.add(_make_entry("wip", "AUTH", ArtifactType.CODE, state="in_flight"))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("list_artifacts", {"state": "in_flight"}))
    ids = [a["artifact_id"] for a in result["artifacts"]]
    assert ids == ["wip"]


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_artifact_id() -> None:
    """
    Given multiple entries,
    when list_artifacts is called with a specific artifact_id,
    then exactly that entry is returned.
    """
    index = ProjectIndex()
    index.add(_make_entry("exact", "AUTH", ArtifactType.CODE))
    index.add(_make_entry("other", "AUTH", ArtifactType.CODE))

    surface = _make_surface(index)
    result = json.loads(await surface.dispatch("list_artifacts", {"artifact_id": "exact"}))
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["artifact_id"] == "exact"


# ---------------------------------------------------------------------------
# start_subagent (stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_subagent_returns_artifact_ids() -> None:
    """
    Given a valid sub-agent name,
    when start_subagent is called,
    then the result contains an artifact_ids list.
    """
    surface = _make_surface()
    result = json.loads(
        await surface.dispatch(
            "start_subagent",
            {"name": "narrative_author", "task_message": "Build a trading bot"},
        )
    )
    assert "artifact_ids" in result
    assert isinstance(result["artifact_ids"], list)


# ---------------------------------------------------------------------------
# run_author_critic_iteration (stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_author_critic_iteration_returns_verdict() -> None:
    """
    Given author and critic names,
    when run_author_critic_iteration is called,
    then the result contains verdict and concerns.
    """
    surface = _make_surface()
    result = json.loads(
        await surface.dispatch(
            "run_author_critic_iteration",
            {
                "author_name": "requirements_author",
                "critic_name": "requirements_critic",
                "input_artifact_ids": [],
            },
        )
    )
    assert "verdict" in result
    assert "concerns" in result
    assert isinstance(result["concerns"], list)


# ---------------------------------------------------------------------------
# request_user_approval — autonomous mode fast-path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_user_approval_autonomous_auto_agrees() -> None:
    """
    Given autonomous mode is on,
    when request_user_approval is called,
    then the result is agree without blocking.
    """
    surface = _make_surface(autonomous=True)
    result = json.loads(
        await surface.dispatch(
            "request_user_approval",
            {"gate_type": "narrative", "summary": "Ready for review"},
        )
    )
    assert result["action"] == "agree"


# ---------------------------------------------------------------------------
# finalize_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_project_sets_session_phase_to_done() -> None:
    """
    When finalize_project is called,
    then the session phase is set to 'done' and the result status is 'done'.
    """
    index = ProjectIndex()
    session = SessionState()
    gate = GateOrchestrator(_make_app_state(), MagicMock())

    async def _stub(name: str, task: str, ids: list[str]) -> list[str]:
        return []

    async def _stub_ac(a: str, c: str, ids: list[str], prev: str | None) -> dict[str, object]:
        return {"artifact_id": None, "verdict": "accepted", "concerns": []}

    async def _stub_rollback(sha: str) -> None:
        pass

    surface = ToolSurface(
        index=index,
        gate=gate,
        session=session,
        run_subagent_fn=_stub,
        run_author_critic_fn=_stub_ac,
        rollback_fn=_stub_rollback,
    )
    result = json.loads(await surface.dispatch("finalize_project", {}))
    assert result["status"] == "done"
    assert session.phase == "done"


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_requires_target_sha() -> None:
    """
    Given an empty tool input,
    when rollback is called,
    then the result is an error.
    """
    surface = _make_surface()
    result = json.loads(await surface.dispatch("rollback", {}))
    assert "error" in result


@pytest.mark.asyncio
async def test_rollback_calls_rollback_fn() -> None:
    """
    Given a valid target_sha,
    when rollback is called,
    then the rollback_fn callback is invoked with the sha.
    """
    called_with: list[str] = []

    async def _capture_rollback(sha: str) -> None:
        called_with.append(sha)

    index = ProjectIndex()
    session = SessionState()
    gate = GateOrchestrator(_make_app_state(), MagicMock())

    async def _stub(name: str, task: str, ids: list[str]) -> list[str]:
        return []

    async def _stub_ac(a: str, c: str, ids: list[str], prev: str | None) -> dict[str, object]:
        return {"artifact_id": None, "verdict": "accepted", "concerns": []}

    surface = ToolSurface(
        index=index,
        gate=gate,
        session=session,
        run_subagent_fn=_stub,
        run_author_critic_fn=_stub_ac,
        rollback_fn=_capture_rollback,
    )
    result = json.loads(await surface.dispatch("rollback", {"target_sha": "abc123"}))
    assert result["status"] == "completed"
    assert called_with == ["abc123"]


# ---------------------------------------------------------------------------
# unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error() -> None:
    """
    When dispatch is called with an unknown tool name,
    then the result contains an error.
    """
    surface = _make_surface()
    result = json.loads(await surface.dispatch("nonexistent_tool", {}))
    assert "error" in result

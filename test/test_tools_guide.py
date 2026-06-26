"""Behavior tests for the guide-facing tools in :mod:`kodo.tools`.

Every agent now shares one :class:`~kodo.tools.ToolDispatcher`; these tests
exercise the tools the guide typically holds — the read-only ones
(``query_frontier``, ``list_artifacts``), the sub-agent launchers
(``run_subagent``, ``run_author_critic_iteration``), and the terminal tools
(``finalize_project``, ``rollback``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.runtime import GateOrchestrator, SessionState
from kodo.tools import ToolDispatcher
from kodo.workspace import ArtifactType, IndexEntry, ProjectIndex

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
        created_at=datetime.now(tz=UTC),
        last_modified=datetime.now(tz=UTC),
    )


def _make_app_state() -> MagicMock:
    state = MagicMock()
    state.send = AsyncMock()
    return state


class _StubServices:
    """Engine-side stub satisfying ``kodo.tools.EngineServices``.

    The sub-agent launchers return canned IDs; ``rollback``/``complete_artifact``
    default to no-ops but accept overrides so a test can assert they were
    invoked.
    """

    def __init__(
        self,
        rollback: Callable[[str], Awaitable[None]] | None = None,
        complete_artifact: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._rollback = rollback
        self._complete = complete_artifact

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        return {"artifact_ids": [f"stub-artifact-{name}"], "summary": "done"}

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        for_revision_artifact_ids: list[str],
    ) -> dict[str, object]:
        return {"artifact_id": f"stub-{author_name}", "verdict": "accepted", "concerns": []}

    async def rollback(self, target_sha: str) -> None:
        if self._rollback is not None:
            await self._rollback(target_sha)

    async def complete_artifact(self, artifact_id: str) -> None:
        if self._complete is not None:
            await self._complete(artifact_id)

    async def disable_autonomous_mode(self) -> None:
        return None

    async def post_update(self, message: str) -> None:
        return None


def _make_dispatcher(
    index: ProjectIndex | None = None,
    autonomous: bool = False,
    session: SessionState | None = None,
    rollback_fn: Callable[[str], Awaitable[None]] | None = None,
) -> ToolDispatcher:
    if index is None:
        index = ProjectIndex()
    if session is None:
        session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous

    return ToolDispatcher(
        workspace=MagicMock(),
        index=index,
        resolver=MagicMock(),
        gate=GateOrchestrator(_make_app_state(), MagicMock()),
        session=session,
        services=_StubServices(rollback=rollback_fn),
        agent_name="guide",
        session_id="sess-test",
    )


# ---------------------------------------------------------------------------
# query_frontier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_frontier_empty_index_returns_empty_list() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(await dispatcher.dispatch("query_frontier", {}))
    assert result == {"frontier": []}


@pytest.mark.asyncio
async def test_query_frontier_reports_first_missing_type() -> None:
    index = ProjectIndex()
    index.add(_make_entry("a1", "AUTH", ArtifactType.FUNCTIONAL_DESIGN))

    dispatcher = _make_dispatcher(index)
    result = json.loads(await dispatcher.dispatch("query_frontier", {}))

    assert len(result["frontier"]) == 1
    entry = result["frontier"][0]
    assert entry["responsibility_code"] == "AUTH"
    assert entry["next_type"] == "test-plan"


@pytest.mark.asyncio
async def test_query_frontier_skips_fully_complete_responsibilities() -> None:
    index = ProjectIndex()
    for artifact_type in (
        ArtifactType.FUNCTIONAL_DESIGN,
        ArtifactType.TEST_PLAN,
        ArtifactType.TEST,
        ArtifactType.CODE,
    ):
        index.add(_make_entry(f"x-{artifact_type.value}", "TRADE", artifact_type))

    dispatcher = _make_dispatcher(index)
    result = json.loads(await dispatcher.dispatch("query_frontier", {}))
    codes = [e["responsibility_code"] for e in result["frontier"]]
    assert "TRADE" not in codes


@pytest.mark.asyncio
async def test_query_frontier_multiple_responsibilities() -> None:
    index = ProjectIndex()
    index.add(_make_entry("a1", "AUTH", ArtifactType.FUNCTIONAL_DESIGN))
    index.add(_make_entry("a2", "AUTH", ArtifactType.TEST_PLAN))
    index.add(_make_entry("b1", "TRADE", ArtifactType.FUNCTIONAL_DESIGN))

    dispatcher = _make_dispatcher(index)
    result = json.loads(await dispatcher.dispatch("query_frontier", {}))

    by_code = {e["responsibility_code"]: e["next_type"] for e in result["frontier"]}
    assert by_code["AUTH"] == "test"
    assert by_code["TRADE"] == "test-plan"


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_artifacts_requires_at_least_one_filter() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(await dispatcher.dispatch("list_artifacts", {}))
    assert "error" in result


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_type() -> None:
    index = ProjectIndex()
    index.add(_make_entry("c1", "AUTH", ArtifactType.CODE))
    index.add(_make_entry("t1", "AUTH", ArtifactType.TEST))

    dispatcher = _make_dispatcher(index)
    result = json.loads(await dispatcher.dispatch("list_artifacts", {"type": "code"}))
    ids = [a["artifact_id"] for a in result["artifacts"]]
    assert ids == ["c1"]


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_responsibility_code() -> None:
    index = ProjectIndex()
    index.add(_make_entry("a1", "AUTH", ArtifactType.CODE))
    index.add(_make_entry("b1", "TRADE", ArtifactType.CODE))

    dispatcher = _make_dispatcher(index)
    result = json.loads(
        await dispatcher.dispatch("list_artifacts", {"responsibility_code": "AUTH"})
    )
    ids = [a["artifact_id"] for a in result["artifacts"]]
    assert ids == ["a1"]


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_state() -> None:
    index = ProjectIndex()
    index.add(_make_entry("done", "AUTH", ArtifactType.CODE, state="completed"))
    index.add(_make_entry("wip", "AUTH", ArtifactType.CODE, state="in_flight"))

    dispatcher = _make_dispatcher(index)
    result = json.loads(await dispatcher.dispatch("list_artifacts", {"state": "in_flight"}))
    ids = [a["artifact_id"] for a in result["artifacts"]]
    assert ids == ["wip"]


@pytest.mark.asyncio
async def test_list_artifacts_filters_by_artifact_id() -> None:
    index = ProjectIndex()
    index.add(_make_entry("exact", "AUTH", ArtifactType.CODE))
    index.add(_make_entry("other", "AUTH", ArtifactType.CODE))

    dispatcher = _make_dispatcher(index)
    result = json.loads(await dispatcher.dispatch("list_artifacts", {"artifact_id": "exact"}))
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["artifact_id"] == "exact"


# ---------------------------------------------------------------------------
# run_subagent / run_author_critic_iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_subagent_returns_artifact_ids() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(
        await dispatcher.dispatch(
            "run_subagent",
            {"name": "narrative_author", "task_input": {"instructions": "Build a trading bot"}},
        )
    )
    assert "artifact_ids" in result
    assert result["artifact_ids"] == ["stub-artifact-narrative_author"]


@pytest.mark.asyncio
async def test_run_author_critic_iteration_returns_verdict() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(
        await dispatcher.dispatch(
            "run_author_critic_iteration",
            {
                "author_name": "requirements_author",
                "critic_name": "requirements_critic",
                "input_artifact_ids": [],
            },
        )
    )
    assert "verdict" in result
    assert isinstance(result["concerns"], list)


class _DenyingServices(_StubServices):
    """Services whose spawn methods reject every call, as the engine gate does."""

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        raise PermissionError(f"Agent {caller!r} is not permitted to spawn sub-agent {name!r}.")

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        for_revision_artifact_ids: list[str],
    ) -> dict[str, object]:
        raise PermissionError(f"Agent {caller!r} is not permitted to spawn {author_name!r}.")


def _make_denying_dispatcher() -> ToolDispatcher:
    session = SessionState()
    return ToolDispatcher(
        workspace=MagicMock(),
        index=ProjectIndex(),
        resolver=MagicMock(),
        gate=GateOrchestrator(_make_app_state(), MagicMock()),
        session=session,
        services=_DenyingServices(),
        agent_name="problem_solver",
        session_id="sess-test",
    )


@pytest.mark.asyncio
async def test_run_subagent_denied_returns_error() -> None:
    dispatcher = _make_denying_dispatcher()
    result = json.loads(
        await dispatcher.dispatch(
            "run_subagent",
            {"name": "narrative_author", "task_input": {"instructions": "go"}},
        )
    )
    assert "artifact_ids" not in result
    assert "not permitted" in result["error"]


@pytest.mark.asyncio
async def test_run_author_critic_iteration_denied_returns_error() -> None:
    dispatcher = _make_denying_dispatcher()
    result = json.loads(
        await dispatcher.dispatch(
            "run_author_critic_iteration",
            {
                "author_name": "architect",
                "critic_name": "architect_critic",
                "input_artifact_ids": [],
            },
        )
    )
    assert "verdict" not in result
    assert "not permitted" in result["error"]


# ---------------------------------------------------------------------------
# finalize_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_project_sets_session_phase_to_done() -> None:
    session = SessionState()
    dispatcher = _make_dispatcher(session=session)
    result = json.loads(await dispatcher.dispatch("finalize_project", {}))
    assert result["status"] == "done"
    assert session.phase == "done"


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_requires_target_sha() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(await dispatcher.dispatch("rollback", {}))
    assert "error" in result


@pytest.mark.asyncio
async def test_rollback_calls_rollback_fn() -> None:
    called_with: list[str] = []

    async def _capture_rollback(sha: str) -> None:
        called_with.append(sha)

    dispatcher = _make_dispatcher(rollback_fn=_capture_rollback)
    result = json.loads(await dispatcher.dispatch("rollback", {"target_sha": "abc123"}))
    assert result["status"] == "completed"
    assert called_with == ["abc123"]


# ---------------------------------------------------------------------------
# unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(await dispatcher.dispatch("nonexistent_tool", {}))
    assert "error" in result

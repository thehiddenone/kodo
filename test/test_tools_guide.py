"""Behavior tests for the guide-facing tools in :mod:`kodo.tools`.

Every agent now shares one :class:`~kodo.tools.ToolDispatcher`; these tests
exercise the tools the guide typically holds — the read-only status scan
(``guided_dev_status``), the sub-agent launchers (``run_subagent``,
``run_author_critic_iteration``), and the terminal tools (``finalize_project``,
``rollback``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.guided_state import append_accepted, append_feedback, append_new_revision
from kodo.runtime import GateOrchestrator, SessionState
from kodo.tools import ToolDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state() -> MagicMock:
    state = MagicMock()
    state.send = AsyncMock()
    return state


class _StubServices:
    """Engine-side stub satisfying ``kodo.tools.EngineServices``.

    The sub-agent launchers return canned values; ``rollback`` defaults to a
    no-op but accepts an override so a test can assert it was invoked.
    """

    def __init__(self, rollback: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._rollback = rollback

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        return {
            "primary_path": f"specs/stub-{name}.md",
            "paths": [f"specs/stub-{name}.md"],
            "summary": "done",
        }

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
        return {
            "path": path or f"specs/stub-{author_name}.md",
            "status": "accepted",
            "concerns": [],
        }

    async def rollback(self, target_sha: str) -> None:
        if self._rollback is not None:
            await self._rollback(target_sha)

    async def disable_autonomous_mode(self) -> None:
        return None

    async def create_project(
        self, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        return {"path": path or f"/tmp/{name}", "name": name}


def _make_dispatcher(
    *,
    mode: str = "guided",
    project_root: Path | None = None,
    autonomous: bool = False,
    session: SessionState | None = None,
    rollback_fn: Callable[[str], Awaitable[None]] | None = None,
) -> ToolDispatcher:
    if session is None:
        session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous

    return ToolDispatcher(
        resolver=MagicMock(),
        gate=GateOrchestrator(_make_app_state(), MagicMock()),
        session=session,
        services=_StubServices(rollback=rollback_fn),
        agent_name="guide",
        session_id="sess-test",
        mode=mode,
        project_root=project_root,
    )


# ---------------------------------------------------------------------------
# guided_dev_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guided_dev_status_no_tracked_files_returns_empty_list(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(project_root=tmp_path)
    result = json.loads(await dispatcher.dispatch("guided_dev_status", {}))
    assert result == {"files": []}


@pytest.mark.asyncio
async def test_guided_dev_status_reports_status_from_last_entry(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    doc = tmp_path / "specs" / "architecture.md"
    doc.write_text("x", encoding="utf-8")
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha1",
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )

    dispatcher = _make_dispatcher(project_root=tmp_path)
    result = json.loads(await dispatcher.dispatch("guided_dev_status", {}))
    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "specs/architecture.md"
    assert result["files"][0]["status"] == "pending_review"
    assert result["files"][0]["last_event"]

    append_feedback(
        doc,
        tmp_path,
        reviewer="architect_critic",
        accept=False,
        concerns=[{"kind": "gap", "description": "x"}],
        summary="needs work",
    )
    result = json.loads(await dispatcher.dispatch("guided_dev_status", {}))
    assert result["files"][0]["status"] == "needs_revision"

    append_feedback(
        doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
    )
    append_accepted(doc, tmp_path)
    result = json.loads(await dispatcher.dispatch("guided_dev_status", {}))
    assert result["files"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_guided_dev_status_errors_outside_guided_mode(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(mode="problem_solving", project_root=tmp_path)
    result = json.loads(await dispatcher.dispatch("guided_dev_status", {}))
    assert "error" in result


# ---------------------------------------------------------------------------
# run_subagent / run_author_critic_iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_subagent_returns_primary_path() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(
        await dispatcher.dispatch(
            "run_subagent",
            {"name": "narrative_author", "task_input": {"instructions": "Build a trading bot"}},
        )
    )
    assert result["primary_path"] == "specs/stub-narrative_author.md"


@pytest.mark.asyncio
async def test_run_author_critic_iteration_returns_status() -> None:
    dispatcher = _make_dispatcher()
    result = json.loads(
        await dispatcher.dispatch(
            "run_author_critic_iteration",
            {
                "author_name": "requirements_author",
                "critic_name": "requirements_critic",
                "instructions": "go",
            },
        )
    )
    assert result["status"] == "accepted"
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
        path: str,
        input_paths: dict[str, str],
        instructions: str,
        for_revision: bool,
    ) -> dict[str, object]:
        raise PermissionError(f"Agent {caller!r} is not permitted to spawn {author_name!r}.")


def _make_denying_dispatcher() -> ToolDispatcher:
    session = SessionState()
    return ToolDispatcher(
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
    assert "primary_path" not in result
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
                "instructions": "go",
            },
        )
    )
    assert "status" not in result
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
    result = json.loads(
        await dispatcher.dispatch(
            "rollback", {"intent": "restore the pre-refactor checkpoint", "target_sha": "abc123"}
        )
    )
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

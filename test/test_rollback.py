"""Behavioral tests for Rollback.

Tests verify: session termination events, workspace cleared, mirror checked out,
project src/gen restored from mirror, sidecar files excluded, index rebuilt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kodo.mirror._promoter import Promoter
from kodo.mirror._repo import MirrorRepo
from kodo.runtime._rollback import Rollback
from kodo.runtime._session_log import SessionLog
from kodo.toolchains._interface import (
    ToolchainBuildResult,
    ToolchainPlugin,
    ToolchainTestResult,
    ToolchainTestScope,
)
from kodo.workspace._models import Artifact, ArtifactType

# ---------------------------------------------------------------------------
# Stub toolchain
# ---------------------------------------------------------------------------


class _StubToolchain(ToolchainPlugin):
    @property
    def name(self) -> str:
        return "stub"

    @property
    def languages(self) -> list[str]:
        return ["stub"]

    async def init(self, project_root: Path) -> None:
        pass

    async def add_dependency(self, name: str, version: str | None = None) -> None:
        pass

    async def build(self, component_dir: Path) -> ToolchainBuildResult:
        return ToolchainBuildResult(success=True, output="")

    async def test(self, scope: ToolchainTestScope) -> ToolchainTestResult:
        return ToolchainTestResult(passed=0, failed=0)

    async def format(self, paths: list[Path]) -> None:
        pass

    def source_filename(self, filename_hint: str) -> str:
        return filename_hint if "." in filename_hint else f"{filename_hint}.py"

    def test_filename(self, filename_hint: str) -> str:
        stem = filename_hint.split(".")[0]
        return f"test_{stem}.py"


_TOOLCHAIN = _StubToolchain()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artifact(
    artifact_id: str,
    artifact_type: ArtifactType,
    content: str = "content",
    filename_hint: str = "out.md",
) -> Artifact:
    return Artifact(
        id=artifact_id,
        type=artifact_type,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        created_at=datetime.now(tz=UTC),
        content=content,
        filename_hint=filename_hint,
    )


async def _setup(tmp_path: Path) -> tuple[MirrorRepo, Rollback, Path]:
    """Return (mirror, rollback, project_root) with an initialised mirror.

    ``project_root`` is ``tmp_path`` itself so ``.kodo/`` lives directly inside it,
    matching the real layout where src/, gen/, and .kodo/ are siblings.
    """
    project_root = tmp_path
    mirror = MirrorRepo(tmp_path / ".kodo" / "checkpoints")
    await mirror.init()
    rollback = Rollback(project_root=project_root, mirror=mirror)
    return mirror, rollback, project_root


async def _promote(tmp_path: Path, mirror: MirrorRepo, artifact: Artifact) -> str:
    promoter = Promoter(project_root=tmp_path, mirror=mirror, toolchain=_TOOLCHAIN)
    return await promoter.promote(artifact, f"[{artifact.type.value}] approved")


# ---------------------------------------------------------------------------
# Step 1 — session termination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_terminates_active_sessions(tmp_path: Path) -> None:
    """
    Given two active SessionLog instances,
    when execute() is called,
    then each session log contains a termination event with the target SHA.
    """
    mirror, rollback, _ = await _setup(tmp_path)
    sha = await mirror.head_sha()

    sessions_dir = tmp_path / ".kodo" / "sessions"
    log_a = SessionLog(sessions_dir, "sess-A")
    log_b = SessionLog(sessions_dir, "sess-B")
    log_a.append({"kind": "session_start"})
    log_b.append({"kind": "session_start"})

    await rollback.execute(sha, active_session_logs=[log_a, log_b])

    events_a = log_a.read_events()
    events_b = log_b.read_events()
    assert any(e.get("event") == "session_terminated_by_rollback" for e in events_a)
    assert any(e.get("event") == "session_terminated_by_rollback" for e in events_b)


@pytest.mark.asyncio
async def test_rollback_termination_event_contains_target_sha(tmp_path: Path) -> None:
    """
    Given one active session log,
    when execute() is called,
    then the termination event records the exact target SHA.
    """
    mirror, rollback, _ = await _setup(tmp_path)
    sha = await mirror.head_sha()

    sessions_dir = tmp_path / ".kodo" / "sessions"
    log = SessionLog(sessions_dir, "sess-X")
    log.append({"kind": "session_start"})

    await rollback.execute(sha, active_session_logs=[log])

    termination = next(
        e for e in log.read_events() if e.get("event") == "session_terminated_by_rollback"
    )
    assert termination["target_sha"] == sha


# ---------------------------------------------------------------------------
# Step 2 — workspace cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_clears_workspace(tmp_path: Path) -> None:
    """
    Given workspace files present,
    when execute() is called,
    then the workspace directory is removed.
    """
    mirror, rollback, _ = await _setup(tmp_path)
    sha = await mirror.head_sha()

    workspace_dir = tmp_path / ".kodo" / "workspace" / "PROJ" / "PROJ"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "art-1.json").write_text('{"id": "art-1"}', encoding="utf-8")

    await rollback.execute(sha)

    assert not (tmp_path / ".kodo" / "workspace").exists()


# ---------------------------------------------------------------------------
# Step 3+4+5 — mirror checkout and project restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_restores_project_files_from_mirror_snapshot(tmp_path: Path) -> None:
    """
    Given a narrative promoted to commit A, then a requirements promoted to commit B,
    when rollback to commit A executes,
    then the project src/ contains only the narrative file (as it was at A).
    """
    mirror, rollback, project_root = await _setup(tmp_path)

    a_narrative = _artifact(
        "art-narr", ArtifactType.NARRATIVE, content="v1", filename_hint="narrative.md"
    )
    sha_a = await _promote(tmp_path, mirror, a_narrative)

    a_req = _artifact(
        "art-req", ArtifactType.REQUIREMENTS, content="REQ-001", filename_hint="req.md"
    )
    await _promote(tmp_path, mirror, a_req)

    await rollback.execute(sha_a)

    narrative_path = project_root / "src" / "narrative" / "narrative.md"
    requirements_path = project_root / "src" / "requirements" / "req.md"

    assert narrative_path.exists()
    assert narrative_path.read_text(encoding="utf-8") == "v1"
    assert not requirements_path.exists()


@pytest.mark.asyncio
async def test_rollback_does_not_copy_sidecar_files_to_project(tmp_path: Path) -> None:
    """
    Given a promoted narrative (which has a sidecar in the mirror),
    when rollback executes,
    then no .kodo.json sidecar files appear in the project src/ tree.
    """
    mirror, rollback, project_root = await _setup(tmp_path)
    sha = await _promote(
        tmp_path, mirror, _artifact("art-1", ArtifactType.NARRATIVE, filename_hint="narrative.md")
    )

    await rollback.execute(sha)

    sidecars = list((project_root / "src").rglob("*.kodo.json"))
    assert sidecars == []


@pytest.mark.asyncio
async def test_rollback_deletes_existing_project_src_gen(tmp_path: Path) -> None:
    """
    Given stale src/ and gen/ directories,
    when rollback to a SHA where those directories were empty executes,
    then src/ and gen/ do not contain the stale files.
    """
    mirror, rollback, project_root = await _setup(tmp_path)
    sha = await mirror.head_sha()

    stale_src = project_root / "src" / "old.md"
    stale_src.parent.mkdir(parents=True)
    stale_src.write_text("stale", encoding="utf-8")

    await rollback.execute(sha)

    assert not stale_src.exists()


# ---------------------------------------------------------------------------
# Step 6 — rebuilt index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_returns_index_with_completed_entries(tmp_path: Path) -> None:
    """
    Given a narrative promoted to commit A,
    when rollback to A runs,
    then the returned index has the narrative as a completed entry.
    """
    mirror, rollback, _ = await _setup(tmp_path)
    await _promote(
        tmp_path,
        mirror,
        _artifact("art-narr", ArtifactType.NARRATIVE, filename_hint="narrative.md"),
    )
    sha = await mirror.head_sha()

    result = await rollback.execute(sha)

    completed = result.index.completed_entries()
    assert any(e.artifact_id == "art-narr" for e in completed)


@pytest.mark.asyncio
async def test_rollback_index_has_no_in_flight_entries(tmp_path: Path) -> None:
    """
    Given workspace files existed before rollback,
    when rollback clears the workspace and rebuilds,
    then the returned index has zero in-flight entries.
    """
    mirror, rollback, _ = await _setup(tmp_path)
    sha = await mirror.head_sha()

    workspace_dir = tmp_path / ".kodo" / "workspace" / "PROJ" / "PROJ"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "art-inflight.json").write_text(
        json.dumps(
            {
                "id": "art-inflight",
                "type": "narrative",
                "author": "agent",
                "project_code": "PROJ",
                "responsibility_code": "PROJ",
                "created_at": datetime.now(tz=UTC).isoformat(),
                "content": "draft",
                "filename_hint": "narrative.md",
                "supersedes": [],
                "requirement_ids": [],
                "reviewed_artifact_id": None,
                "verdict": None,
                "concerns": [],
                "metadata": {},
                "session_id": "sess-1",
            }
        ),
        encoding="utf-8",
    )

    result = await rollback.execute(sha)

    assert result.index.in_flight_entries() == []

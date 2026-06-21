"""Behavioral tests for ProjectIndex and ProjectBootstrap.

Tests verify the three-phase bootstrap: mirror scan (completed), workspace scan
(in-flight), and classify by session presence (orphan deletion, broken lineage).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kodo.runtime import ProjectBootstrap, locate_orchestrator_session
from kodo.toolchains import (
    ToolchainBuildResult,
    ToolchainPlugin,
    ToolchainTestResult,
    ToolchainTestScope,
)
from kodo.workspace import Artifact, ArtifactType, IndexEntry, MirrorRepo, ProjectIndex, Promoter

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
    responsibility_code: str = "PROJ",
    supersedes: list[str] | None = None,
    session_id: str | None = "sess-1",
) -> Artifact:
    return Artifact(
        id=artifact_id,
        type=artifact_type,
        author="agent",
        project_code="PROJ",
        responsibility_code=responsibility_code,
        created_at=datetime.now(tz=UTC),
        content=content,
        filename_hint=filename_hint,
        supersedes=supersedes or [],
        session_id=session_id,
    )


def _bootstrap(tmp_path: Path) -> ProjectBootstrap:
    return ProjectBootstrap(
        mirror_dir=tmp_path / ".kodo" / "checkpoints",
        workspace_dir=tmp_path / ".kodo" / "workspace",
        sessions_dir=tmp_path / ".kodo" / "sessions",
    )


def _locate_session(tmp_path: Path) -> tuple[str, bool]:
    return locate_orchestrator_session(
        marker_dir=tmp_path / ".kodo",
        sessions_dir=tmp_path / ".kodo" / "sessions",
    )


async def _promoted_mirror(tmp_path: Path, artifact: Artifact) -> None:
    """Initialise the mirror and promote one artifact into it."""
    mirror = MirrorRepo(tmp_path / ".kodo" / "checkpoints")
    if not mirror.is_initialized():
        await mirror.init()
    promoter = Promoter(
        project_root=tmp_path / "project",
        mirror=mirror,
        toolchain=_TOOLCHAIN,
    )
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    await promoter.promote(artifact, f"[{artifact.type.value}] approved")


def _write_workspace_artifact(tmp_path: Path, artifact: Artifact) -> Path:
    """Write an in-flight artifact JSON to the workspace directory."""
    ws_dir = tmp_path / ".kodo" / "workspace" / artifact.project_code / artifact.responsibility_code
    ws_dir.mkdir(parents=True, exist_ok=True)
    path = ws_dir / f"{artifact.id}.json"
    data = {
        "id": artifact.id,
        "type": artifact.type.value,
        "author": artifact.author,
        "project_code": artifact.project_code,
        "responsibility_code": artifact.responsibility_code,
        "created_at": artifact.created_at.isoformat(),
        "content": artifact.content,
        "requirement_ids": artifact.requirement_ids,
        "filename_hint": artifact.filename_hint,
        "supersedes": artifact.supersedes,
        "reviewed_artifact_id": None,
        "verdict": None,
        "concerns": [],
        "metadata": {},
        "session_id": artifact.session_id,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _write_session_log(tmp_path: Path, session_id: str, main_session: str = "main-1") -> None:
    """Create a minimal subsession JSONL log under a main session directory.

    In-flight artifacts are stamped with the *subsession* ID of the sub-agent
    that produced them; the orphan check looks for that subsession log under
    ``sessions/<main>/subsessions/<subsession_id>.jsonl``.
    """
    subsessions_dir = tmp_path / ".kodo" / "sessions" / main_session / "subsessions"
    subsessions_dir.mkdir(parents=True, exist_ok=True)
    (subsessions_dir / f"{session_id}.jsonl").write_text(
        json.dumps({"role": "user", "content": "task"}) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# ProjectIndex unit tests
# ---------------------------------------------------------------------------


def test_project_index_add_and_get_by_id(tmp_path: Path) -> None:
    """
    Given an IndexEntry added to ProjectIndex,
    when get_by_id() is called with its artifact_id,
    then the same entry is returned.
    """
    index = ProjectIndex()
    entry = IndexEntry(
        artifact_id="id-1",
        project_code="PROJ",
        responsibility_code="PROJ",
        type=ArtifactType.NARRATIVE,
        state="completed",
        location=tmp_path / "src" / "narrative" / "narrative.md",
        filename_hint="narrative.md",
        supersedes=[],
        requirement_ids=[],
        session_id=None,
        author="agent",
        created_at=datetime.now(tz=UTC),
        last_modified=datetime.now(tz=UTC),
    )
    index.add(entry)
    assert index.get_by_id("id-1") is entry


def test_project_index_get_by_key_returns_matching_entries(tmp_path: Path) -> None:
    """
    Given two entries with the same (project_code, responsibility_code, type),
    when get_by_key() is called,
    then both are returned.
    """
    index = ProjectIndex()

    def make(aid: str) -> IndexEntry:
        return IndexEntry(
            artifact_id=aid,
            project_code="PROJ",
            responsibility_code="AUTH",
            type=ArtifactType.CODE,
            state="completed",
            location=tmp_path / f"{aid}.py",
            filename_hint=f"{aid}.py",
            supersedes=[],
            requirement_ids=[],
            session_id=None,
            author="coder",
            created_at=datetime.now(tz=UTC),
            last_modified=datetime.now(tz=UTC),
        )

    index.add(make("id-a"))
    index.add(make("id-b"))
    results = index.get_by_key("PROJ", "AUTH", ArtifactType.CODE)
    assert {e.artifact_id for e in results} == {"id-a", "id-b"}


def test_project_index_remove_makes_entry_absent(tmp_path: Path) -> None:
    """
    Given an entry in the index,
    when remove() is called,
    then get_by_id() returns None.
    """
    index = ProjectIndex()
    entry = IndexEntry(
        artifact_id="id-x",
        project_code="PROJ",
        responsibility_code="PROJ",
        type=ArtifactType.NARRATIVE,
        state="in_flight",
        location=tmp_path / "ws.json",
        filename_hint="narrative.md",
        supersedes=[],
        requirement_ids=[],
        session_id="sess-1",
        author="agent",
        created_at=datetime.now(tz=UTC),
        last_modified=datetime.now(tz=UTC),
    )
    index.add(entry)
    index.remove("id-x")
    assert index.get_by_id("id-x") is None


# ---------------------------------------------------------------------------
# Phase 1 — mirror scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_phase1_completed_entry_from_promoted_artifact(tmp_path: Path) -> None:
    """
    Given a promoted narrative artifact (content + sidecar in mirror),
    when bootstrap runs,
    then the index contains one completed entry with correct fields.
    """
    a = _artifact("art-1", ArtifactType.NARRATIVE, filename_hint="narrative.md")
    await _promoted_mirror(tmp_path, a)

    index = _bootstrap(tmp_path).build_index()

    entries = index.completed_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e.artifact_id == "art-1"
    assert e.type == ArtifactType.NARRATIVE
    assert e.state == "completed"
    assert e.author == "agent"
    assert e.session_id == "sess-1"


@pytest.mark.asyncio
async def test_bootstrap_phase1_multiple_artifacts_all_appear(tmp_path: Path) -> None:
    """
    Given two promoted artifacts,
    when bootstrap runs,
    then the index contains two completed entries.
    """
    a1 = _artifact("art-1", ArtifactType.NARRATIVE, filename_hint="narrative.md")
    a2 = _artifact("art-2", ArtifactType.REQUIREMENTS, filename_hint="requirements.md")
    mirror = MirrorRepo(tmp_path / ".kodo" / "checkpoints")
    await mirror.init()
    (tmp_path / "project").mkdir(parents=True)
    promoter = Promoter(project_root=tmp_path / "project", mirror=mirror, toolchain=_TOOLCHAIN)
    await promoter.promote(a1, "narrative approved")
    await promoter.promote(a2, "requirements approved")

    index = _bootstrap(tmp_path).build_index()

    ids = {e.artifact_id for e in index.completed_entries()}
    assert "art-1" in ids
    assert "art-2" in ids


@pytest.mark.asyncio
async def test_bootstrap_phase1_empty_mirror_gives_empty_index(tmp_path: Path) -> None:
    """
    Given an initialised but empty mirror (only init commit),
    when bootstrap runs,
    then the completed entries list is empty.
    """
    mirror = MirrorRepo(tmp_path / ".kodo" / "checkpoints")
    await mirror.init()

    index = _bootstrap(tmp_path).build_index()

    assert index.completed_entries() == []


# ---------------------------------------------------------------------------
# Phase 2 — workspace scan
# ---------------------------------------------------------------------------


def test_bootstrap_phase2_in_flight_entry_from_workspace(tmp_path: Path) -> None:
    """
    Given a workspace artifact JSON with a valid session log,
    when bootstrap runs,
    then the index contains one in-flight entry with correct fields.
    """
    a = _artifact(
        "art-3", ArtifactType.NARRATIVE, filename_hint="narrative.md", session_id="sess-99"
    )
    _write_workspace_artifact(tmp_path, a)
    _write_session_log(tmp_path, "sess-99")

    index = _bootstrap(tmp_path).build_index()

    entries = index.in_flight_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e.artifact_id == "art-3"
    assert e.type == ArtifactType.NARRATIVE
    assert e.state == "in_flight"
    assert e.session_id == "sess-99"


# ---------------------------------------------------------------------------
# Phase 3 — classify in-flight entries
# ---------------------------------------------------------------------------


def test_bootstrap_phase3_orphan_is_removed_from_index(tmp_path: Path) -> None:
    """
    Given an in-flight workspace artifact whose session log does NOT exist,
    when bootstrap runs,
    then the entry is absent from the index (orphan deleted).
    """
    a = _artifact(
        "art-orphan",
        ArtifactType.NARRATIVE,
        filename_hint="narrative.md",
        session_id="missing-sess",
    )
    ws_path = _write_workspace_artifact(tmp_path, a)
    # No session log written.

    index = _bootstrap(tmp_path).build_index()

    assert index.get_by_id("art-orphan") is None
    assert not ws_path.exists()


def test_bootstrap_phase3_orphan_with_no_session_id_is_removed(tmp_path: Path) -> None:
    """
    Given an in-flight artifact with no session_id at all,
    when bootstrap runs,
    then the entry is classified as orphan and removed.
    """
    a = _artifact(
        "art-nosess", ArtifactType.NARRATIVE, filename_hint="narrative.md", session_id=None
    )
    ws_path = _write_workspace_artifact(tmp_path, a)

    index = _bootstrap(tmp_path).build_index()

    assert index.get_by_id("art-nosess") is None
    assert not ws_path.exists()


def test_bootstrap_phase3_resumable_entry_remains_in_index(tmp_path: Path) -> None:
    """
    Given an in-flight artifact whose session log exists,
    when bootstrap runs,
    then the entry remains in the index.
    """
    a = _artifact(
        "art-ok", ArtifactType.NARRATIVE, filename_hint="narrative.md", session_id="sess-ok"
    )
    _write_workspace_artifact(tmp_path, a)
    _write_session_log(tmp_path, "sess-ok")

    index = _bootstrap(tmp_path).build_index()

    assert index.get_by_id("art-ok") is not None


# ---------------------------------------------------------------------------
# Phase 3 — broken supersedes lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_phase3_broken_lineage_drops_in_flight(tmp_path: Path) -> None:
    """
    Given a completed entry in the mirror and an in-flight entry that claims to
    supersede a non-existent artifact (not the completed entry's ID),
    when bootstrap runs,
    then the in-flight entry is dropped (conservative resolution).
    """
    # Promote art-completed into the mirror
    a_completed = _artifact(
        "art-completed", ArtifactType.NARRATIVE, filename_hint="narrative.md", session_id=None
    )
    await _promoted_mirror(tmp_path, a_completed)

    # In-flight entry supersedes a wrong ID, not "art-completed"
    a_inflight = _artifact(
        "art-inflight",
        ArtifactType.NARRATIVE,
        filename_hint="narrative.md",
        supersedes=["art-some-other-id"],  # broken: doesn't reference art-completed
        session_id="sess-ok",
    )
    _write_workspace_artifact(tmp_path, a_inflight)
    _write_session_log(tmp_path, "sess-ok")

    index = _bootstrap(tmp_path).build_index()

    assert index.get_by_id("art-completed") is not None
    assert index.get_by_id("art-inflight") is None


@pytest.mark.asyncio
async def test_bootstrap_phase3_correct_lineage_keeps_in_flight(tmp_path: Path) -> None:
    """
    Given a completed entry and an in-flight entry that correctly supersedes it,
    when bootstrap runs,
    then both entries are in the index.
    """
    a_completed = _artifact(
        "art-v1", ArtifactType.NARRATIVE, filename_hint="narrative.md", session_id=None
    )
    await _promoted_mirror(tmp_path, a_completed)

    a_inflight = _artifact(
        "art-v2",
        ArtifactType.NARRATIVE,
        filename_hint="narrative.md",
        supersedes=["art-v1"],  # correctly references the completed entry
        session_id="sess-ok",
    )
    _write_workspace_artifact(tmp_path, a_inflight)
    _write_session_log(tmp_path, "sess-ok")

    index = _bootstrap(tmp_path).build_index()

    assert index.get_by_id("art-v1") is not None
    assert index.get_by_id("art-v2") is not None


# ---------------------------------------------------------------------------
# Orchestrator session location (locate_orchestrator_session)
# ---------------------------------------------------------------------------


def test_session_fresh_project_creates_marker(tmp_path: Path) -> None:
    """
    Given a project with no orchestrator.session marker,
    when the session is located,
    then a marker file is created and resumed is False.
    """
    (tmp_path / ".kodo").mkdir(parents=True)
    session_id, resumed = _locate_session(tmp_path)
    marker_path = tmp_path / ".kodo" / "orchestrator.session"

    assert not resumed
    assert session_id != ""
    assert marker_path.exists()
    assert marker_path.read_text(encoding="utf-8").strip() == session_id


def test_session_existing_session_log_is_resumed(tmp_path: Path) -> None:
    """
    Given a marker pointing to an existing session log,
    when the session is located,
    then the same session_id is returned and resumed is True.
    """
    # Simulate a prior run: write marker + create a session log
    kodo_dir = tmp_path / ".kodo"
    sessions_dir = kodo_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    session_id = "prior-session-id"
    (kodo_dir / "orchestrator.session").write_text(session_id + "\n", encoding="utf-8")
    (sessions_dir / session_id).mkdir()

    located_id, resumed = _locate_session(tmp_path)

    assert resumed
    assert located_id == session_id


def test_session_missing_session_log_starts_fresh(tmp_path: Path) -> None:
    """
    Given a marker pointing to a session ID with no matching log dir,
    when the session is located,
    then the anomaly is handled: marker is cleared, a fresh session is created,
    and resumed is False.
    """
    kodo_dir = tmp_path / ".kodo"
    kodo_dir.mkdir(parents=True)
    stale_id = "stale-session-id"
    (kodo_dir / "orchestrator.session").write_text(stale_id + "\n", encoding="utf-8")
    # NOTE: no matching sessions/stale-session-id

    session_id, resumed = _locate_session(tmp_path)

    assert not resumed
    assert session_id != stale_id
    # Marker was overwritten with the fresh session id
    marker_path = kodo_dir / "orchestrator.session"
    assert marker_path.read_text(encoding="utf-8").strip() == session_id


def test_session_two_consecutive_locates_resume(tmp_path: Path) -> None:
    """
    Given two consecutive session locations on the same workspace,
    when the second finds the marker + log from the first,
    then the second reports resumed=True with the same id.
    """
    (tmp_path / ".kodo").mkdir(parents=True)
    # First call — creates the marker
    first_id, _ = _locate_session(tmp_path)
    # Simulate the session directory being created (as TransientStore does via attach_session)
    sessions_dir = tmp_path / ".kodo" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / first_id).mkdir()

    # Second call — should resume
    second_id, resumed = _locate_session(tmp_path)

    assert resumed
    assert second_id == first_id

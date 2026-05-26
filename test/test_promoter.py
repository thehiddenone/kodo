"""Behavioral tests for Promoter.

Tests verify that promote() writes files to both the project directory and the
mirror working tree, creates a git commit, and raises on non-materialized types.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kodo.mirror._promoter import Promoter, PromoterError
from kodo.mirror._repo import MirrorRepo
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


def _artifact(
    type: ArtifactType, content: str = "content", filename_hint: str = "out.md"
) -> Artifact:
    return Artifact(
        id="test-id",
        type=type,
        author="agent",
        project_code="PROJ",
        responsibility_code="PROJ",
        created_at=datetime.now(tz=UTC),
        content=content,
        filename_hint=filename_hint,
    )


async def _make_promoter(tmp_path: Path) -> tuple[Promoter, MirrorRepo, Path]:
    """Return (promoter, mirror, project_root) with an initialised mirror."""
    mirror_dir = tmp_path / ".kodo" / "checkpoints"
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True)
    mirror = MirrorRepo(mirror_dir)
    await mirror.init()
    promoter = Promoter(project_root=project_root, mirror=mirror, toolchain=_TOOLCHAIN)
    return promoter, mirror, project_root


# ---------------------------------------------------------------------------
# promote() writes to the project directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_writes_artifact_to_project_dir(tmp_path: Path) -> None:
    """
    Given a narrative artifact with content,
    when promote() is called,
    then the file appears at src/narrative/<filename_hint> under project_root.
    """
    promoter, _, project_root = await _make_promoter(tmp_path)
    a = _artifact(ArtifactType.NARRATIVE, content="# Narrative", filename_hint="narrative.md")

    await promoter.promote(a, "[narrative] approved")

    expected = project_root / "src" / "narrative" / "narrative.md"
    assert expected.exists()
    assert expected.read_text(encoding="utf-8") == "# Narrative"


# ---------------------------------------------------------------------------
# promote() writes to the mirror working tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_writes_artifact_to_mirror_tree(tmp_path: Path) -> None:
    """
    Given a narrative artifact,
    when promote() is called,
    then the same relative path appears inside the mirror working tree.
    """
    promoter, mirror, _ = await _make_promoter(tmp_path)
    a = _artifact(ArtifactType.NARRATIVE, content="# Narrative", filename_hint="narrative.md")

    await promoter.promote(a, "[narrative] approved")

    mirror_file = mirror.repo_dir / "src" / "narrative" / "narrative.md"
    assert mirror_file.exists()
    assert mirror_file.read_text(encoding="utf-8") == "# Narrative"


# ---------------------------------------------------------------------------
# promote() creates a mirror commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_creates_mirror_commit(tmp_path: Path) -> None:
    """
    Given an initialised mirror with only the init commit,
    when promote() is called,
    then the mirror log contains the promotion commit.
    """
    promoter, mirror, _ = await _make_promoter(tmp_path)
    a = _artifact(ArtifactType.NARRATIVE, filename_hint="narrative.md")

    await promoter.promote(a, "[narrative] approved")

    commits = await mirror.log()
    messages = [c.message for c in commits]
    assert "[narrative] approved" in messages


@pytest.mark.asyncio
async def test_promote_returns_40_char_sha(tmp_path: Path) -> None:
    """
    Given an artifact,
    when promote() is called,
    then the return value is a 40-character hex SHA.
    """
    promoter, _, _ = await _make_promoter(tmp_path)
    a = _artifact(ArtifactType.REQUIREMENTS, filename_hint="req.md")

    sha = await promoter.promote(a, "[requirements] approved")

    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


# ---------------------------------------------------------------------------
# promote() raises for non-materialized types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_raises_for_feedback_artifact(tmp_path: Path) -> None:
    """
    Given a feedback artifact (not materialized),
    when promote() is called,
    then PromoterError is raised.
    """
    promoter, _, _ = await _make_promoter(tmp_path)
    a = _artifact(ArtifactType.FEEDBACK, filename_hint="fb.md")

    with pytest.raises(PromoterError):
        await promoter.promote(a, "should fail")

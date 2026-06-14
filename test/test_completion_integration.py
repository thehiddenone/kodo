"""Integration test for the artifact completion (promotion) path.

Composes the real Workspace, ProjectIndex, MirrorRepo, and Promoter the way the
engine's ``__complete_artifact`` does, and verifies the observable outcome:
content materialized to the project tree, committed to the mirror with a
sidecar, the staging file removed, and the index entry flipped to completed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.mirror._promoter import Promoter
from kodo.mirror._repo import MirrorRepo
from kodo.toolchains._interface import (
    ToolchainBuildResult,
    ToolchainPlugin,
    ToolchainTestResult,
    ToolchainTestScope,
)
from kodo.workspace import ArtifactType, ProjectIndex, Workspace
from kodo.workspace._materialization import materialization_path


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
        return f"test_{filename_hint.split('.')[0]}.py"


@pytest.mark.asyncio
async def test_completion_promotes_and_moves_out_of_workspace(tmp_path: Path) -> None:
    project_root = tmp_path
    index = ProjectIndex()
    workspace = Workspace(project_root, index)

    mirror = MirrorRepo(project_root / ".kodo" / "checkpoints")
    await mirror.init()
    toolchain = _StubToolchain()
    promoter = Promoter(project_root=project_root, mirror=mirror, toolchain=toolchain)

    artifact_id = await workspace.publish(
        artifact_type=ArtifactType.NARRATIVE,
        author="narrative_author",
        project_code="PROJ",
        responsibility_code="PROJ",
        content="# Narrative",
        filename_hint="narrative.md",
    )
    staging = index.get_by_id(artifact_id)
    assert staging is not None and staging.location.exists()

    # Mirror the engine's __complete_artifact sequence.
    artifact = (await workspace.read(artifact_id=artifact_id))[0]
    target = materialization_path(artifact, project_root, toolchain)
    assert target is not None
    await promoter.promote(artifact, "[narrative] PROJ completed")
    await workspace.mark_completed(artifact_id, location=target)

    # Materialized to the project tree, committed to the mirror with a sidecar.
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "# Narrative"
    mirror_file = mirror.repo_dir / "src" / "narrative" / "narrative.md"
    assert mirror_file.exists()
    assert (mirror_file.parent / (mirror_file.name + ".kodo.json")).exists()

    # Moved out of staging; index reports it completed at the promoted location.
    assert not staging.location.exists()
    entry = index.get_by_id(artifact_id)
    assert entry is not None
    assert entry.state == "completed"
    assert entry.location == target

    # And query-side reads reflect the new state.
    stable = await workspace.read(
        project_code="PROJ", artifact_type=ArtifactType.NARRATIVE, version="stable"
    )
    assert artifact_id in {a.id for a in stable}

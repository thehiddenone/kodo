"""Maps artifact type and codenames to materialized paths in src/ and gen/."""

from __future__ import annotations

import asyncio
from pathlib import Path

from kodo.toolchains._interface import ToolchainPlugin

from ._models import Artifact, ArtifactType


def materialization_path(
    artifact: Artifact,
    project_root: Path,
    toolchain: ToolchainPlugin,
) -> Path | None:
    """Return the path where content should be materialized, or None.

    Feedback artifacts are not materialized. All other types write into
    ``src/`` (specification artifacts) or ``gen/`` (code and test artifacts).
    File names for ``CODE`` and ``TEST`` artifacts are derived via the
    supplied toolchain so that extensions and naming conventions are
    language-appropriate.

    Args:
        artifact (Artifact): The artifact to place.
        project_root (Path): Root directory of the Kodo project.
        toolchain (ToolchainPlugin): Active toolchain, used to derive
            language-appropriate file names for code and test artifacts.

    Returns:
        Path | None: Destination path, or ``None`` for types that are not
        materialized (e.g. ``feedback``).
    """
    match artifact.type:
        case ArtifactType.NARRATIVE:
            return project_root / "src" / "narrative.kd"
        case ArtifactType.ARCHITECTURE:
            return project_root / "src" / "responsibilities.kd"
        case ArtifactType.DESIGN_PLAN:
            return project_root / "src" / "design_plan.kd"
        case ArtifactType.TECH_STACK:
            return project_root / "src" / "tech_stack.kd"
        case ArtifactType.REQUIREMENTS:
            return project_root / "src" / artifact.responsibility_code / "requirements.kd"
        case ArtifactType.FUNCTIONAL_DESIGN:
            return project_root / "src" / artifact.responsibility_code / "design.kd"
        case ArtifactType.TEST_PLAN:
            return project_root / "src" / artifact.responsibility_code / "test_plan.kd"
        case ArtifactType.CODE:
            leaf = toolchain.source_filename(artifact.filename_hint or artifact.id)
            return project_root / "gen" / artifact.responsibility_code / leaf
        case ArtifactType.TEST:
            leaf = toolchain.test_filename(artifact.filename_hint or artifact.id)
            return project_root / "gen" / artifact.responsibility_code / "tests" / leaf
        case _:
            return None


async def materialize(
    artifact: Artifact,
    project_root: Path,
    toolchain: ToolchainPlugin,
) -> None:
    """Write artifact content to its conventional src/ or gen/ path.

    Does nothing for artifact types that are not materialized or when
    ``artifact.content`` is ``None``.

    Args:
        artifact (Artifact): The artifact to write. Must have content loaded.
        project_root (Path): Root directory of the Kodo project.
        toolchain (ToolchainPlugin): Active toolchain for file name derivation.
    """
    target = materialization_path(artifact, project_root, toolchain)
    if target is None or artifact.content is None:
        return
    await asyncio.to_thread(_write, target, artifact.content)


async def dematerialize(
    artifact: Artifact,
    project_root: Path,
    toolchain: ToolchainPlugin,
) -> None:
    """Remove the materialized file for a retiring artifact.

    Does nothing for artifact types that are not materialized.

    Args:
        artifact (Artifact): The artifact being retired.
        project_root (Path): Root directory of the Kodo project.
        toolchain (ToolchainPlugin): Active toolchain for file name derivation.
    """
    target = materialization_path(artifact, project_root, toolchain)
    if target is None:
        return
    await asyncio.to_thread(_delete_if_exists, target)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _delete_if_exists(path: Path) -> None:
    path.unlink(missing_ok=True)

"""Maps artifact type and codenames to materialized paths in src/ and gen/."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ._models import Artifact, ArtifactType


def materialization_path(artifact: Artifact, project_root: Path) -> Path | None:
    """Return the path where content should be materialized, or None.

    Feedback artifacts are not materialized. All other types write into
    ``src/`` (specification artifacts) or ``gen/`` (code and test artifacts).

    Args:
        artifact (Artifact): The artifact to place.
        project_root (Path): Root directory of the Kodo project.

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
            leaf = artifact.filename_hint or f"{artifact.id}.py"
            return project_root / "gen" / artifact.responsibility_code / leaf
        case ArtifactType.TEST:
            leaf = artifact.filename_hint or f"{artifact.id}_test.py"
            return project_root / "gen" / artifact.responsibility_code / "tests" / leaf
        case _:
            return None


async def materialize(artifact: Artifact, project_root: Path) -> None:
    """Write artifact content to its conventional src/ or gen/ path.

    Does nothing for artifact types that are not materialized or when
    ``artifact.content`` is ``None``.

    Args:
        artifact (Artifact): The artifact to write. Must have content loaded.
        project_root (Path): Root directory of the Kodo project.
    """
    target = materialization_path(artifact, project_root)
    if target is None or artifact.content is None:
        return
    await asyncio.to_thread(_write, target, artifact.content)


async def dematerialize(artifact: Artifact, project_root: Path) -> None:
    """Remove the materialized file for a retiring artifact.

    Does nothing for artifact types that are not materialized.

    Args:
        artifact (Artifact): The artifact being retired.
        project_root (Path): Root directory of the Kodo project.
    """
    target = materialization_path(artifact, project_root)
    if target is None:
        return
    await asyncio.to_thread(_delete_if_exists, target)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _delete_if_exists(path: Path) -> None:
    path.unlink(missing_ok=True)

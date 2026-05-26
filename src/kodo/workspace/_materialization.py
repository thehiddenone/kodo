"""Maps artifact type and codenames to materialized paths in src/ and gen/.

Path layout per STATE_AND_LIFECYCLE.md §1.1:

    src/narrative/          narrative artifacts
    src/tech_stack/         tech-stack artifacts
    src/requirements/       requirements artifacts
    src/architecture/       architecture artifacts
    src/design/             design-plan artifacts (project-wide)
    src/design/<comp>/      functional-design artifacts (per-component)
    src/test_design/<comp>/ test-plan artifacts (per-component)
    gen/src/<comp>/         code artifacts (per-component)
    gen/test/<comp>/        test artifacts (per-component)

``<comp>`` is the snake_case component directory derived from the
component's display name via :class:`ComponentRegistry`.  When no
registry is supplied (or the component is not yet declared), the raw
``responsibility_code`` is used as a fallback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kodo.toolchains._interface import ToolchainPlugin

from ._component_registry import ComponentRegistry
from ._models import Artifact, ArtifactType


def materialization_path(
    artifact: Artifact,
    project_root: Path,
    toolchain: ToolchainPlugin,
    registry: ComponentRegistry | None = None,
) -> Path | None:
    """Return the path where content should be materialized, or None.

    Feedback artifacts are never materialized.  All other types land in
    ``src/`` (specification artifacts) or ``gen/`` (code and test artifacts)
    according to the §1.1 layout.

    Args:
        artifact (Artifact): The artifact to place.
        project_root (Path): Root directory of the Kodo project.
        toolchain (ToolchainPlugin): Active toolchain, used to derive
            language-appropriate file names for code and test artifacts.
        registry (ComponentRegistry | None): Component registry for
            codename→component_dir lookups.  Falls back to the raw
            ``responsibility_code`` when ``None`` or the codename is unknown.

    Returns:
        Path | None: Destination path, or ``None`` for types that are not
        materialized (e.g. ``feedback``).
    """
    reg = registry or ComponentRegistry.empty()
    hint = artifact.filename_hint or artifact.id

    match artifact.type:
        case ArtifactType.NARRATIVE:
            return project_root / "src" / "narrative" / hint
        case ArtifactType.TECH_STACK:
            return project_root / "src" / "tech_stack" / hint
        case ArtifactType.REQUIREMENTS:
            return project_root / "src" / "requirements" / hint
        case ArtifactType.ARCHITECTURE:
            return project_root / "src" / "architecture" / hint
        case ArtifactType.DESIGN_PLAN:
            return project_root / "src" / "design" / hint
        case ArtifactType.FUNCTIONAL_DESIGN:
            comp = reg.component_dir(artifact.responsibility_code)
            return project_root / "src" / "design" / comp / hint
        case ArtifactType.TEST_PLAN:
            comp = reg.component_dir(artifact.responsibility_code)
            return project_root / "src" / "test_design" / comp / hint
        case ArtifactType.CODE:
            comp = reg.component_dir(artifact.responsibility_code)
            leaf = toolchain.source_filename(hint)
            return project_root / "gen" / "src" / comp / leaf
        case ArtifactType.TEST:
            comp = reg.component_dir(artifact.responsibility_code)
            leaf = toolchain.test_filename(hint)
            return project_root / "gen" / "test" / comp / leaf
        case _:
            return None


async def materialize(
    artifact: Artifact,
    project_root: Path,
    toolchain: ToolchainPlugin,
    registry: ComponentRegistry | None = None,
) -> Path | None:
    """Write artifact content to its conventional src/ or gen/ path.

    Returns the path the artifact was written to, or ``None`` when the
    artifact type is not materialized or content is absent.

    Args:
        artifact (Artifact): The artifact to write. Must have content loaded.
        project_root (Path): Root directory of the Kodo project.
        toolchain (ToolchainPlugin): Active toolchain for file name derivation.
        registry (ComponentRegistry | None): Component registry for path lookup.

    Returns:
        Path | None: The path written, or ``None``.
    """
    target = materialization_path(artifact, project_root, toolchain, registry)
    if target is None or artifact.content is None:
        return None
    await asyncio.to_thread(_write, target, artifact.content)
    return target


async def dematerialize(
    artifact: Artifact,
    project_root: Path,
    toolchain: ToolchainPlugin,
    registry: ComponentRegistry | None = None,
) -> None:
    """Remove the materialized file for a retiring artifact.

    Does nothing for artifact types that are not materialized.

    Args:
        artifact (Artifact): The artifact being retired.
        project_root (Path): Root directory of the Kodo project.
        toolchain (ToolchainPlugin): Active toolchain for file name derivation.
        registry (ComponentRegistry | None): Component registry for path lookup.
    """
    target = materialization_path(artifact, project_root, toolchain, registry)
    if target is None:
        return
    await asyncio.to_thread(_delete_if_exists, target)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _delete_if_exists(path: Path) -> None:
    path.unlink(missing_ok=True)

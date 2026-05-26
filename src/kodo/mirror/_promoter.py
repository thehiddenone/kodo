"""Artifact promotion: writes accepted artifacts to project dir and mirror."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kodo.toolchains._interface import ToolchainPlugin
from kodo.workspace._component_registry import ComponentRegistry
from kodo.workspace._materialization import materialization_path
from kodo.workspace._models import Artifact

from ._repo import MirrorRepo

_SIDECAR_SUFFIX = ".kodo.json"


class PromoterError(Exception):
    """Raised when an artifact cannot be promoted."""


class Promoter:
    """Writes accepted artifacts to the project directory and the mirror.

    The caller is responsible for supplying an artifact whose ``content``
    field is populated.  ``Promoter`` derives the destination paths from the
    artifact type and component registry, writes the file to both the live
    project tree and the mirror working tree, then calls
    :meth:`MirrorRepo.stage_and_commit` to create a checkpoint commit.

    Args:
        project_root (Path): Root directory of the Kodo project.
        mirror (MirrorRepo): The mirror git repository.
        toolchain (ToolchainPlugin): Active toolchain for filename derivation.
        registry (ComponentRegistry | None): Component registry for path
            lookup.  Falls back to the raw responsibility_code when ``None``.
    """

    __project_root: Path
    __mirror: MirrorRepo
    __toolchain: ToolchainPlugin
    __registry: ComponentRegistry | None

    def __init__(
        self,
        project_root: Path,
        mirror: MirrorRepo,
        toolchain: ToolchainPlugin,
        registry: ComponentRegistry | None = None,
    ) -> None:
        """Initialise the Promoter.

        Args:
            project_root (Path): Root directory of the Kodo project.
            mirror (MirrorRepo): The mirror git repository.
            toolchain (ToolchainPlugin): Active toolchain for filename derivation.
            registry (ComponentRegistry | None): Component registry for path
                lookup.
        """
        self.__project_root = project_root
        self.__mirror = mirror
        self.__toolchain = toolchain
        self.__registry = registry

    async def promote(self, artifact: Artifact, message: str) -> str:
        """Write artifact to the project dir and mirror, then commit.

        The file is written to its §1.1 path under ``project_root`` and to the
        same relative path inside the mirror working tree.  A git commit is
        created in the mirror regardless of whether any other files changed.

        Args:
            artifact (Artifact): The accepted artifact to promote.  Must have
                ``content`` populated.
            message (str): Git commit message for the mirror commit.

        Returns:
            str: The 40-character SHA of the resulting mirror commit.

        Raises:
            PromoterError: The artifact type is not materialized (e.g.,
                ``feedback``), or ``content`` is absent.
        """
        target = materialization_path(
            artifact, self.__project_root, self.__toolchain, self.__registry
        )
        if target is None or artifact.content is None:
            raise PromoterError(
                f"Artifact {artifact.id!r} of type {artifact.type!r} cannot be promoted"
            )

        rel = target.relative_to(self.__project_root)
        mirror_target = self.__mirror.repo_dir / rel

        await asyncio.to_thread(_write, target, artifact.content)
        await asyncio.to_thread(_write, mirror_target, artifact.content)

        # Write sidecar metadata to mirror only (project tree stays clean).
        sidecar = Path(str(mirror_target) + _SIDECAR_SUFFIX)
        await asyncio.to_thread(_write_sidecar, sidecar, artifact)

        return await self.__mirror.stage_and_commit(message)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_sidecar(path: Path, artifact: Artifact) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "artifact_id": artifact.id,
        "project_code": artifact.project_code,
        "responsibility_code": artifact.responsibility_code,
        "type": artifact.type.value,
        "author": artifact.author,
        "filename_hint": artifact.filename_hint or "",
        "supersedes": artifact.supersedes,
        "requirement_ids": artifact.requirement_ids,
        "session_id": artifact.session_id,
        "created_at": artifact.created_at.isoformat(),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

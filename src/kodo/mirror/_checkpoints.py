"""Checkpoint commit logic — snapshot src/ + gen/ on every approval gate."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ._repo import CheckpointInfo, MirrorRepo

if TYPE_CHECKING:
    from kodo.project._layout import ProjectLayout

__all__ = ["CheckpointManager"]

_log = logging.getLogger(__name__)


class CheckpointManager:
    """Creates mirror commits at every approval gate.

    Args:
        layout: Project layout supplying the ``src/``, ``gen/``, and
            ``.kodo/checkpoints/`` paths.
    """

    def __init__(self, layout: ProjectLayout) -> None:
        from kodo.project._layout import ProjectLayout as _PL  # noqa: F401 (TYPE_CHECKING import)

        self.__layout = layout
        self.__repo = MirrorRepo(layout.checkpoints_dir)

    async def ensure_initialized(self) -> None:
        """Initialise the mirror repository if it does not exist yet."""
        if not self.__repo.is_initialized():
            await self.__repo.init()

    async def create_checkpoint(
        self,
        gate_type: str,
        component: str | None = None,
    ) -> str:
        """Snapshot the project and create a commit.

        Args:
            gate_type: Gate type string (e.g. ``'narrative'``).
            component: Optional component name for per-component gates.

        Returns:
            str: The new commit SHA.
        """
        label = gate_type if component is None else f"{gate_type}/{component}"
        message = f"[{label}] approved"
        sha = await self.__repo.sync_and_commit(
            src_dir=self.__layout.src_dir,
            gen_dir=self.__layout.gen_dir,
            message=message,
        )
        _log.info("Checkpoint: %s → %s", message, sha[:8])
        return sha

    async def list_checkpoints(self) -> list[CheckpointInfo]:
        """Return all checkpoint commits newest-first."""
        return await self.__repo.log()

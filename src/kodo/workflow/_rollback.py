"""Rollback: restore the project to a prior checkpoint commit.

Rollback procedure (§8.3 of STATE_AND_LIFECYCLE.md):

1. Terminate all active sessions — append a termination event to each session log.
2. Clear ``.kodo/workspace/`` entirely.
3. ``MirrorRepo.checkout(target_sha)`` — mirror working tree reflects the target snapshot.
4. Delete ``<project>/src/`` and ``<project>/gen/``.
5. Copy mirror's ``src/`` and ``gen/`` into the project, skipping sidecar files.
6. Rebuild the in-memory index via :class:`ProjectBootstrap`.

The caller is responsible for halting any running sub-agent processes before
calling :meth:`Rollback.execute`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from kodo.mirror._repo import MirrorRepo
from kodo.workflow._session_log import SessionLog

from ._bootstrap import ProjectBootstrap
from ._index import ProjectIndex

_log = logging.getLogger(__name__)

_SIDECAR_SUFFIX = ".kodo.json"
_TERMINATION_EVENT_KIND = "session_terminated_by_rollback"


class Rollback:
    """Restores a Kodo project to a prior mirror checkpoint.

    Args:
        project_root (Path): Root directory of the Kodo project.
        mirror (MirrorRepo): The mirror git repository for this project.
    """

    __project_root: Path
    __mirror: MirrorRepo

    def __init__(self, project_root: Path, mirror: MirrorRepo) -> None:
        """Initialise the rollback handler.

        Args:
            project_root (Path): Root directory of the Kodo project.
            mirror (MirrorRepo): The mirror git repository.
        """
        self.__project_root = project_root
        self.__mirror = mirror

    async def execute(
        self,
        target_sha: str,
        active_session_logs: list[SessionLog] | None = None,
    ) -> ProjectIndex:
        """Execute the full rollback procedure and return the rebuilt index.

        Args:
            target_sha (str): Commit SHA to roll back to (full or abbreviated).
            active_session_logs (list[SessionLog] | None): Session logs of
                all currently active sub-agent sessions.  Each is closed with
                a termination event before the rollback proceeds.

        Returns:
            ProjectIndex: Rebuilt index from the restored on-disk state.
        """
        sessions = active_session_logs or []

        self.__step1_terminate_sessions(sessions, target_sha)

        workspace_dir = self.__project_root / ".kodo" / "workspace"
        await asyncio.to_thread(self.__step2_clear_workspace, workspace_dir)

        await self.__mirror.checkout(target_sha)
        _log.info("Rollback: mirror checked out to %s", target_sha[:8])

        await asyncio.to_thread(self.__step4_delete_project_trees)

        await asyncio.to_thread(self.__step5_copy_from_mirror)

        index = await asyncio.to_thread(self.__step6_rebuild_index, workspace_dir)
        _log.info("Rollback complete: %d completed entries", len(index.completed_entries()))
        return index

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    @staticmethod
    def __step1_terminate_sessions(sessions: list[SessionLog], target_sha: str) -> None:
        for session in sessions:
            session.append(
                {
                    "direction": "engine",
                    "event": _TERMINATION_EVENT_KIND,
                    "target_sha": target_sha,
                }
            )
            _log.info("Rollback: terminated session %s", session.session_id)

    @staticmethod
    def __step2_clear_workspace(workspace_dir: Path) -> None:
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
            _log.info("Rollback: workspace cleared")

    def __step4_delete_project_trees(self) -> None:
        for name in ("src", "gen"):
            tree = self.__project_root / name
            if tree.exists():
                shutil.rmtree(tree)
                _log.info("Rollback: deleted %s/", name)

    def __step5_copy_from_mirror(self) -> None:
        mirror_root = self.__mirror.repo_dir
        for name in ("src", "gen"):
            mirror_tree = mirror_root / name
            project_tree = self.__project_root / name
            if not mirror_tree.exists():
                continue
            project_tree.mkdir(parents=True, exist_ok=True)
            for src_file in mirror_tree.rglob("*"):
                if not src_file.is_file():
                    continue
                if src_file.name.endswith(_SIDECAR_SUFFIX):
                    continue
                rel = src_file.relative_to(mirror_tree)
                dst = project_tree / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)
        _log.info("Rollback: project trees restored from mirror")

    def __step6_rebuild_index(self, workspace_dir: Path) -> ProjectIndex:
        bootstrap = ProjectBootstrap(
            mirror_dir=self.__mirror.repo_dir,
            workspace_dir=workspace_dir,
            sessions_dir=self.__project_root / ".kodo" / "sessions",
        )
        return bootstrap.run()

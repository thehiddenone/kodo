"""Rollback: restore the project to a prior checkpoint commit.

Rollback procedure (STATE_AND_LIFECYCLE.md §8.3):

1. Terminate all active sessions (sub-agent + Orchestrator) — append a
   termination event to each session log.
2. Clear ``.kodo/workspace/`` entirely.
3. ``MirrorRepo.checkout(target_sha)`` — mirror working tree reflects the
   target snapshot.
4. Delete ``<project>/src/`` and ``<project>/gen/``.
5. Copy mirror's ``src/`` and ``gen/`` into the project, skipping sidecar files.
6. Rebuild the full artifact index via :class:`ProjectBootstrap`.

The session identity is owned by the driving session (one per VS Code window)
and is unchanged by a rollback — the engine resets its in-memory conversation
itself.  The caller is responsible for halting any running sub-agent processes
before calling :meth:`Rollback.execute`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from kodo.project import ProjectLayout
from kodo.workspace import MirrorRepo, ProjectIndex

from ._bootstrap import ProjectBootstrap
from ._session_log import SessionLog

_log = logging.getLogger(__name__)

_SIDECAR_SUFFIX = ".kodo.json"
_TERMINATION_EVENT_KIND = "session_terminated_by_rollback"


class Rollback:
    """Restores a Kodo project to a prior mirror checkpoint.

    Args:
        project_root (Path): Root directory of the Kodo project.
        mirror (MirrorRepo): The mirror git repository for this project.
        sessions_dir (Path): Global ``~/.kodo/sessions/`` dir — used only for
            in-flight artifact orphan classification during the index rebuild.
    """

    __project_root: Path
    __mirror: MirrorRepo
    __sessions_dir: Path

    def __init__(self, project_root: Path, mirror: MirrorRepo, sessions_dir: Path) -> None:
        """Initialise the rollback handler.

        Args:
            project_root (Path): Root directory of the Kodo project.
            mirror (MirrorRepo): The mirror git repository.
            sessions_dir (Path): Global session-stores directory.
        """
        self.__project_root = project_root
        self.__mirror = mirror
        self.__sessions_dir = sessions_dir

    async def execute(
        self,
        target_sha: str,
        active_session_logs: list[SessionLog] | None = None,
    ) -> ProjectIndex:
        """Execute the full rollback procedure and return the rebuilt index.

        Args:
            target_sha (str): Commit SHA to roll back to (full or abbreviated).
            active_session_logs (list[SessionLog] | None): Session logs of all
                currently active sessions (sub-agent and Orchestrator).  Each is
                closed with a termination event before the rollback proceeds.

        Returns:
            ProjectIndex: Rebuilt artifact index for the restored snapshot.
        """
        layout = ProjectLayout(self.__project_root)
        sessions = active_session_logs or []

        self.__step1_terminate_sessions(sessions, target_sha)

        await asyncio.to_thread(self.__step3_clear_workspace, layout.workspace_dir)

        await self.__mirror.checkout(target_sha)
        _log.info("Rollback: mirror checked out to %s", target_sha[:8])

        await asyncio.to_thread(self.__step5_delete_project_trees)
        await asyncio.to_thread(self.__step6_copy_from_mirror)

        index = await asyncio.to_thread(self.__step7_rebuild, layout)
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
    def __step3_clear_workspace(workspace_dir: Path) -> None:
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
            _log.info("Rollback: workspace cleared")

    def __step5_delete_project_trees(self) -> None:
        for name in ("src", "gen"):
            tree = self.__project_root / name
            if tree.exists():
                shutil.rmtree(tree)
                _log.info("Rollback: deleted %s/", name)

    def __step6_copy_from_mirror(self) -> None:
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

    def __step7_rebuild(self, layout: ProjectLayout) -> ProjectIndex:
        return ProjectBootstrap(
            mirror_dir=self.__mirror.repo_dir,
            workspace_dir=layout.workspace_dir,
            sessions_dir=self.__sessions_dir,
        ).build_index()

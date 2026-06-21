"""Rollback: restore the project to a prior checkpoint commit.

Rollback procedure (STATE_AND_LIFECYCLE.md §8.3):

1. Terminate all active sessions (sub-agent + Orchestrator) — append a
   termination event to each session log.
2. Clear the Orchestrator session marker (Phase 4 of bootstrap will create
   a fresh session).
3. Clear ``.kodo/workspace/`` entirely.
4. ``MirrorRepo.checkout(target_sha)`` — mirror working tree reflects the
   target snapshot.
5. Delete ``<project>/src/`` and ``<project>/gen/``.
6. Copy mirror's ``src/`` and ``gen/`` into the project, skipping sidecar files.
7. Rebuild the full index via :class:`ProjectBootstrap` (all four phases).
   Phase 4 creates a fresh Orchestrator session because the marker was cleared
   in step 2.

The caller is responsible for halting any running sub-agent processes before
calling :meth:`Rollback.execute`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from kodo.project import ProjectLayout, WorkspaceLayout
from kodo.workspace import MirrorRepo

from ._bootstrap import BootstrapResult, ProjectBootstrap, locate_orchestrator_session
from ._orchestrator import OrchestratorMarker
from ._session_log import SessionLog

_log = logging.getLogger(__name__)

_SIDECAR_SUFFIX = ".kodo.json"
_TERMINATION_EVENT_KIND = "session_terminated_by_rollback"


class Rollback:
    """Restores a Kodo project to a prior mirror checkpoint.

    Args:
        project_root (Path): Root directory of the Kodo project.
        mirror (MirrorRepo): The mirror git repository for this project.
        workspace (WorkspaceLayout): Workspace-tier layout — supplies the
            session marker + ``sessions/`` dir (both workspace-scoped).
    """

    __project_root: Path
    __mirror: MirrorRepo
    __workspace: WorkspaceLayout

    def __init__(self, project_root: Path, mirror: MirrorRepo, workspace: WorkspaceLayout) -> None:
        """Initialise the rollback handler.

        Args:
            project_root (Path): Root directory of the Kodo project.
            mirror (MirrorRepo): The mirror git repository.
            workspace (WorkspaceLayout): Workspace-tier layout.
        """
        self.__project_root = project_root
        self.__mirror = mirror
        self.__workspace = workspace

    async def execute(
        self,
        target_sha: str,
        active_session_logs: list[SessionLog] | None = None,
    ) -> BootstrapResult:
        """Execute the full rollback procedure and return a fresh BootstrapResult.

        Args:
            target_sha (str): Commit SHA to roll back to (full or abbreviated).
            active_session_logs (list[SessionLog] | None): Session logs of all
                currently active sessions (sub-agent and Orchestrator).  Each is
                closed with a termination event before the rollback proceeds.

        Returns:
            BootstrapResult: Rebuilt index and fresh Orchestrator session info.
        """
        layout = ProjectLayout(self.__project_root)
        sessions = active_session_logs or []

        self.__step1_terminate_sessions(sessions, target_sha)

        OrchestratorMarker(self.__workspace.marker_dir).clear()
        _log.info("Rollback: Orchestrator session marker cleared")

        await asyncio.to_thread(self.__step3_clear_workspace, layout.workspace_dir)

        await self.__mirror.checkout(target_sha)
        _log.info("Rollback: mirror checked out to %s", target_sha[:8])

        await asyncio.to_thread(self.__step5_delete_project_trees)
        await asyncio.to_thread(self.__step6_copy_from_mirror)

        result = await asyncio.to_thread(self.__step7_rebuild, layout)
        _log.info(
            "Rollback complete: %d completed entries, new orchestrator session=%s",
            len(result.index.completed_entries()),
            result.orchestrator_session_id[:8],
        )
        return result

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

    def __step7_rebuild(self, layout: ProjectLayout) -> BootstrapResult:
        index = ProjectBootstrap(
            mirror_dir=self.__mirror.repo_dir,
            workspace_dir=layout.workspace_dir,
            sessions_dir=self.__workspace.sessions_dir,
        ).build_index()
        # The marker was cleared in step 2, so this creates a fresh session.
        session_id, resumed = locate_orchestrator_session(
            self.__workspace.marker_dir, self.__workspace.sessions_dir
        )
        return BootstrapResult(
            index=index,
            orchestrator_session_id=session_id,
            orchestrator_resumed=resumed,
        )

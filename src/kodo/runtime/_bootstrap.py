"""Cold-start index population for Kodo projects.

Bootstrap runs in four deterministic phases on every server start
(STATE_AND_LIFECYCLE.md §3):

1. Scan the mirror working tree — produces ``state='completed'`` entries.
2. Scan the workspace — produces ``state='in_flight'`` entries.
3. Classify in-flight entries by session presence — orphans are deleted.
4. Locate or create the Orchestrator session marker.

The result is a :class:`BootstrapResult` containing a fully populated
:class:`ProjectIndex` and the current Orchestrator session ID.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kodo.state._transient import _new_session_id
from kodo.workspace._models import ArtifactType

from ._index import IndexEntry, ProjectIndex
from ._orchestrator import OrchestratorMarker

_log = logging.getLogger(__name__)

_SIDECAR_SUFFIX = ".kodo.json"


@dataclass(frozen=True)
class BootstrapResult:
    """Result of a completed bootstrap run.

    Attributes:
        index: Fully populated in-memory artifact index.
        orchestrator_session_id: The current Orchestrator session ID —
            either resumed from the marker or freshly created.
        orchestrator_resumed: ``True`` when an existing session log was
            found; ``False`` when a fresh session was started.
    """

    index: ProjectIndex
    orchestrator_session_id: str
    orchestrator_resumed: bool


class ProjectBootstrap:
    """Four-phase cold-start bootstrap.

    Args:
        mirror_dir (Path): Root of the mirror working tree
            (``<project>/.kodo/checkpoints/``).
        workspace_dir (Path): Root of the workspace directory
            (``<project>/.kodo/workspace/``).
        sessions_dir (Path): Directory holding session JSONL files
            (``<project>/.kodo/sessions/``).
        kodo_dir (Path): The ``.kodo/`` directory (holds the orchestrator
            marker file and other engine state).
    """

    __mirror_dir: Path
    __workspace_dir: Path
    __sessions_dir: Path
    __kodo_dir: Path

    def __init__(
        self,
        mirror_dir: Path,
        workspace_dir: Path,
        sessions_dir: Path,
        kodo_dir: Path,
    ) -> None:
        """Initialise bootstrap with directory paths.

        Args:
            mirror_dir (Path): Mirror working tree root.
            workspace_dir (Path): Workspace root.
            sessions_dir (Path): Session logs directory.
            kodo_dir (Path): The project's ``.kodo/`` directory.
        """
        self.__mirror_dir = mirror_dir
        self.__workspace_dir = workspace_dir
        self.__sessions_dir = sessions_dir
        self.__kodo_dir = kodo_dir

    def run(self) -> BootstrapResult:
        """Execute all four bootstrap phases and return the result.

        Returns:
            BootstrapResult: Populated index and Orchestrator session info.
        """
        index = ProjectIndex()
        self.__phase1_scan_mirror(index)
        self.__phase2_scan_workspace(index)
        self.__phase3_classify_in_flight(index)
        session_id, resumed = self.__phase4_orchestrator_session()
        return BootstrapResult(
            index=index,
            orchestrator_session_id=session_id,
            orchestrator_resumed=resumed,
        )

    # ------------------------------------------------------------------
    # Phase 1 — mirror scan
    # ------------------------------------------------------------------

    def __phase1_scan_mirror(self, index: ProjectIndex) -> None:
        if not self.__mirror_dir.exists():
            return
        for sidecar in self.__mirror_dir.rglob(f"*{_SIDECAR_SUFFIX}"):
            entry = self.__entry_from_sidecar(sidecar)
            if entry is not None:
                index.add(entry)
                _log.debug("Phase 1: completed %s (%s)", entry.artifact_id[:8], entry.type.value)

    def __entry_from_sidecar(self, sidecar: Path) -> IndexEntry | None:
        content_path = Path(str(sidecar)[: -len(_SIDECAR_SUFFIX)])
        if not content_path.exists():
            _log.warning("Sidecar %s has no matching content file — skipping", sidecar)
            return None
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            mtime = datetime.fromtimestamp(content_path.stat().st_mtime, tz=UTC)
            sup_raw = data.get("supersedes")
            req_raw = data.get("requirement_ids")
            return IndexEntry(
                artifact_id=str(data["artifact_id"]),
                project_code=str(data["project_code"]),
                responsibility_code=str(data["responsibility_code"]),
                type=ArtifactType(str(data["type"])),
                state="completed",
                location=content_path,
                filename_hint=str(data.get("filename_hint") or content_path.name),
                supersedes=[str(s) for s in sup_raw] if isinstance(sup_raw, list) else [],
                requirement_ids=[str(r) for r in req_raw] if isinstance(req_raw, list) else [],
                session_id=str(data["session_id"]) if data.get("session_id") else None,
                author=str(data.get("author") or ""),
                last_modified=mtime,
            )
        except Exception:
            _log.exception("Phase 1: failed to parse sidecar %s — skipping", sidecar)
            return None

    # ------------------------------------------------------------------
    # Phase 2 — workspace scan
    # ------------------------------------------------------------------

    def __phase2_scan_workspace(self, index: ProjectIndex) -> None:
        if not self.__workspace_dir.exists():
            return
        for json_file in self.__workspace_dir.rglob("*.json"):
            if _SIDECAR_SUFFIX in json_file.name:
                continue
            if ".retired" in json_file.parts:
                continue
            if json_file.name in ("index.json",):
                continue
            entry = self.__entry_from_workspace_file(json_file)
            if entry is not None:
                index.add(entry)
                _log.debug("Phase 2: in-flight %s (%s)", entry.artifact_id[:8], entry.type.value)

    def __entry_from_workspace_file(self, path: Path) -> IndexEntry | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            sup_raw = data.get("supersedes")
            req_raw = data.get("requirement_ids")
            return IndexEntry(
                artifact_id=str(data["id"]),
                project_code=str(data["project_code"]),
                responsibility_code=str(data["responsibility_code"]),
                type=ArtifactType(str(data["type"])),
                state="in_flight",
                location=path,
                filename_hint=str(data.get("filename_hint") or ""),
                supersedes=[str(s) for s in sup_raw] if isinstance(sup_raw, list) else [],
                requirement_ids=[str(r) for r in req_raw] if isinstance(req_raw, list) else [],
                session_id=str(data["session_id"]) if data.get("session_id") else None,
                author=str(data.get("author") or ""),
                last_modified=mtime,
            )
        except Exception:
            _log.exception("Phase 2: failed to parse workspace file %s — skipping", path)
            return None

    # ------------------------------------------------------------------
    # Phase 3 — classify in-flight entries
    # ------------------------------------------------------------------

    def __phase3_classify_in_flight(self, index: ProjectIndex) -> None:
        completed_ids = {e.artifact_id for e in index.completed_entries()}

        for entry in list(index.in_flight_entries()):
            if self.__is_orphan(entry):
                self.__drop_entry(index, entry, reason="session_log_absent")
                continue
            if self.__has_broken_lineage(entry, index, completed_ids):
                self.__drop_entry(index, entry, reason="broken_lineage")

    def __is_orphan(self, entry: IndexEntry) -> bool:
        if entry.session_id is None:
            return True
        session_file = self.__sessions_dir / f"{entry.session_id}.jsonl"
        return not session_file.exists()

    def __has_broken_lineage(
        self,
        entry: IndexEntry,
        index: ProjectIndex,
        completed_ids: set[str],
    ) -> bool:
        if not entry.supersedes:
            return False
        related_completed = [
            e
            for e in index.completed_entries()
            if e.project_code == entry.project_code
            and e.responsibility_code == entry.responsibility_code
            and e.type == entry.type
            and e.filename_hint == entry.filename_hint
        ]
        if not related_completed:
            return False
        related_completed_ids = {e.artifact_id for e in related_completed}
        return not any(sid in related_completed_ids for sid in entry.supersedes)

    def __drop_entry(self, index: ProjectIndex, entry: IndexEntry, reason: str) -> None:
        _log.warning(
            "Phase 3: dropping in-flight %s (%s, reason=%s)",
            entry.artifact_id[:8],
            entry.type.value,
            reason,
        )
        index.remove(entry.artifact_id)
        entry.location.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Phase 4 — Orchestrator session (STATE_AND_LIFECYCLE.md §3 Phase 4)
    # ------------------------------------------------------------------

    def __phase4_orchestrator_session(self) -> tuple[str, bool]:
        """Locate or create the Orchestrator session.

        Returns:
            tuple[str, bool]: ``(session_id, resumed)`` where ``resumed``
            is ``True`` if an existing session directory was found.
        """
        marker = OrchestratorMarker(self.__kodo_dir)
        existing = marker.read()

        if existing:
            session_dir = self.__sessions_dir / existing
            if session_dir.is_dir():
                _log.info("Phase 4: Orchestrator session resumed: %s", existing)
                return existing, True
            _log.warning(
                "Phase 4: Orchestrator marker points to missing session dir %s "
                "— discarding marker and starting fresh",
                existing,
            )
            marker.clear()

        session_id = _new_session_id()
        marker.write(session_id)
        _log.info("Phase 4: Orchestrator session started: %s", session_id)
        return session_id, False

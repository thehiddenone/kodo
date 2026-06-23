"""Cold-start index population for Kodo projects.

Bootstrap splits across the two layout tiers (see the ``project-kodo`` memory,
WorkspaceLayout two-root model):

* :func:`locate_guide_session` — the **workspace** tier.  Locates or
  creates the session from the workspace-level marker + ``sessions/`` dir.  Runs
  at server start, before any project is bound.
* :class:`ProjectBootstrap` — the **project** tier.  Rebuilds the artifact
  :class:`ProjectIndex` from the bound project's mirror + workspace dirs.  Runs
  when the current project is lazily bound (Guided) and after a rollback.

Project-tier phases (STATE_AND_LIFECYCLE.md §3):

1. Scan the mirror working tree — produces ``state='completed'`` entries.
2. Scan the workspace — produces ``state='in_flight'`` entries.
3. Classify in-flight entries by session presence — orphans are deleted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kodo.state import new_session_id
from kodo.workspace import ArtifactType, IndexEntry, ProjectIndex, Verdict

from ._guide import GuideMarker

_log = logging.getLogger(__name__)

_SIDECAR_SUFFIX = ".kodo.json"


def locate_guide_session(marker_dir: Path, sessions_dir: Path) -> tuple[str, bool]:
    """Locate or create the Guide session (workspace tier).

    The session is workspace-scoped (mode-agnostic — Guide and Problem
    Solver share it), so its marker and store live under
    ``.kodo-workspace/`` regardless of which project is later bound.

    Args:
        marker_dir (Path): Directory holding the guide session marker
            (``.kodo-workspace/``).
        sessions_dir (Path): Directory holding per-session stores
            (``.kodo-workspace/sessions/``).

    Returns:
        tuple[str, bool]: ``(session_id, resumed)`` where ``resumed`` is
        ``True`` if an existing session directory was found.
    """
    marker = GuideMarker(marker_dir)
    existing = marker.read()

    if existing:
        session_dir = sessions_dir / existing
        if session_dir.is_dir():
            _log.info("Guide session resumed: %s", existing)
            return existing, True
        _log.warning(
            "Guide marker points to missing session dir %s — discarding marker and starting fresh",
            existing,
        )
        marker.clear()

    session_id = new_session_id()
    marker.write(session_id)
    _log.info("Guide session started: %s", session_id)
    return session_id, False


@dataclass(frozen=True)
class BootstrapResult:
    """Result of a completed bootstrap run.

    Attributes:
        index: Fully populated in-memory artifact index.
        guide_session_id: The current Guide session ID —
            either resumed from the marker or freshly created.
        guide_resumed: ``True`` when an existing session log was
            found; ``False`` when a fresh session was started.
    """

    index: ProjectIndex
    guide_session_id: str
    guide_resumed: bool


class ProjectBootstrap:
    """Project-tier cold-start index rebuild (phases 1–3).

    Args:
        mirror_dir (Path): Root of the mirror working tree
            (``<project>/.kodo/checkpoints/``).
        workspace_dir (Path): Root of the workspace directory
            (``<project>/.kodo/workspace/``).
        sessions_dir (Path): Directory holding per-session stores
            (``.kodo-workspace/sessions/``) — used only for orphan
            classification (phase 3).
    """

    __mirror_dir: Path
    __workspace_dir: Path
    __sessions_dir: Path

    def __init__(
        self,
        mirror_dir: Path,
        workspace_dir: Path,
        sessions_dir: Path,
    ) -> None:
        """Initialise bootstrap with directory paths.

        Args:
            mirror_dir (Path): Mirror working tree root.
            workspace_dir (Path): Workspace root.
            sessions_dir (Path): Session stores directory (workspace tier).
        """
        self.__mirror_dir = mirror_dir
        self.__workspace_dir = workspace_dir
        self.__sessions_dir = sessions_dir

    def build_index(self) -> ProjectIndex:
        """Execute the three project-tier phases and return the index.

        Returns:
            ProjectIndex: Populated in-memory artifact index.
        """
        index = ProjectIndex()
        self.__phase1_scan_mirror(index)
        self.__phase2_scan_workspace(index)
        self.__phase3_classify_in_flight(index)
        return index

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
                created_at=(
                    datetime.fromisoformat(str(data["created_at"]))
                    if data.get("created_at")
                    else mtime
                ),
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
                created_at=(
                    datetime.fromisoformat(str(data["created_at"]))
                    if data.get("created_at")
                    else mtime
                ),
                last_modified=mtime,
                verdict=Verdict(str(data["verdict"])) if data.get("verdict") else None,
                reviewed_artifact_id=(
                    str(data["reviewed_artifact_id"]) if data.get("reviewed_artifact_id") else None
                ),
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
        # In-flight artifacts are stamped with the subsession ID of the sub-agent
        # that published them. The subsession log lives under the guide
        # session's ``subsessions/`` directory; an artifact whose subsession log
        # is gone has no producing run and is an orphan.
        if not self.__sessions_dir.is_dir():
            return True
        for session_dir in self.__sessions_dir.iterdir():
            if (session_dir / "subsessions" / f"{entry.session_id}.jsonl").exists():
                return False
        return True

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

"""Project artifact index — the single in-memory source of truth.

``ProjectIndex`` is the authoritative catalog of every artifact in a project
and its lifecycle ``state`` (``in_flight`` while authors/critics work on it in
the workspace staging area; ``completed`` once it has passed all gates and been
moved out). It is constructed once at bootstrap by scanning what is on disk and
then maintained in memory at runtime — the :class:`~kodo.workspace.Workspace`
updates it on every mutation, and the guide reads it (``query_frontier``,
``list_artifacts``).

The index itself is **never persisted**: it is a reflection of on-disk state.
Everything needed to reconstruct it must live on disk — in-flight artifacts as
JSON files under ``.kodo/workspace/``, completed artifacts as committed files
plus sidecars in the mirror tree. It holds artifact **metadata only**; content
stays on disk at :attr:`IndexEntry.location` and is read on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from ._models import ArtifactType, Verdict

__all__ = ["ArtifactState", "IndexEntry", "ProjectIndex"]

ArtifactState = Literal["completed", "in_flight"]


@dataclass(frozen=True)
class IndexEntry:
    """One artifact's metadata record in the project index.

    Attributes:
        artifact_id: Primary key.
        project_code: PROJECTCODE (e.g. ``ETRD``).
        responsibility_code: Component codename (e.g. ``AUTH``).
        type: Artifact type.
        state: Whether the artifact is completed or in-flight.
        location: Absolute path to the artifact file on disk (content lives
            here; the index never holds content).
        filename_hint: Leaf filename (stable across superseding revisions).
        supersedes: Prior artifact IDs replaced by this one.
        requirement_ids: Requirements covered by this artifact.
        session_id: Session that produced this artifact (set for in-flight).
        author: Sub-agent name that published the artifact.
        created_at: Artifact creation timestamp (from the artifact record).
        verdict: Feedback verdict, when the artifact is a feedback artifact.
        reviewed_artifact_id: Target artifact, when this is a feedback artifact.
        last_modified: File modification time.
    """

    artifact_id: str
    project_code: str
    responsibility_code: str
    type: ArtifactType
    state: ArtifactState
    location: Path
    filename_hint: str
    supersedes: list[str]
    requirement_ids: list[str]
    session_id: str | None
    author: str
    created_at: datetime
    last_modified: datetime
    verdict: Verdict | None = None
    reviewed_artifact_id: str | None = None


class ProjectIndex:
    """In-memory index of all project artifacts (completed + in-flight).

    The primary key is ``artifact_id``.  Secondary lookups by
    ``(project_code, responsibility_code, type)`` return lists because a
    component may have multiple artifacts of the same type (e.g., several
    ``code`` files).
    """

    __by_id: dict[str, IndexEntry]

    def __init__(self) -> None:
        """Create an empty index."""
        self.__by_id = {}

    def add(self, entry: IndexEntry) -> None:
        """Insert or replace an entry.

        Args:
            entry (IndexEntry): The entry to add.
        """
        self.__by_id[entry.artifact_id] = entry

    def remove(self, artifact_id: str) -> None:
        """Remove an entry by artifact ID (no-op if absent).

        Args:
            artifact_id (str): ID of the entry to remove.
        """
        self.__by_id.pop(artifact_id, None)

    def mark_completed(self, artifact_id: str, location: Path | None = None) -> IndexEntry | None:
        """Transition an entry to ``completed`` state, optionally relocating it.

        Args:
            artifact_id (str): ID of the entry to complete.
            location (Path | None): New on-disk location after promotion. When
                ``None``, the existing location is kept.

        Returns:
            IndexEntry | None: The updated entry, or ``None`` if no such entry.
        """
        entry = self.__by_id.get(artifact_id)
        if entry is None:
            return None
        updated = replace(
            entry,
            state="completed",
            location=location if location is not None else entry.location,
        )
        self.__by_id[artifact_id] = updated
        return updated

    def get_by_id(self, artifact_id: str) -> IndexEntry | None:
        """Return the entry for the given artifact ID, or ``None``.

        Args:
            artifact_id (str): Artifact primary key.

        Returns:
            IndexEntry | None: Matching entry, or ``None``.
        """
        return self.__by_id.get(artifact_id)

    def get_by_key(
        self,
        project_code: str,
        responsibility_code: str,
        artifact_type: ArtifactType,
    ) -> list[IndexEntry]:
        """Return all entries matching the given triple.

        Args:
            project_code (str): Project code filter.
            responsibility_code (str): Component code filter.
            artifact_type (ArtifactType): Type filter.

        Returns:
            list[IndexEntry]: All matching entries (may be empty).
        """
        return [
            e
            for e in self.__by_id.values()
            if e.project_code == project_code
            and e.responsibility_code == responsibility_code
            and e.type == artifact_type
        ]

    def all_entries(self) -> list[IndexEntry]:
        """Return every entry in the index.

        Returns:
            list[IndexEntry]: All entries, in insertion order.
        """
        return list(self.__by_id.values())

    def completed_entries(self) -> list[IndexEntry]:
        """Return entries whose state is ``'completed'``.

        Returns:
            list[IndexEntry]: Completed entries.
        """
        return [e for e in self.__by_id.values() if e.state == "completed"]

    def in_flight_entries(self) -> list[IndexEntry]:
        """Return entries whose state is ``'in_flight'``.

        Returns:
            list[IndexEntry]: In-flight entries.
        """
        return [e for e in self.__by_id.values() if e.state == "in_flight"]

    def __contains__(self, artifact_id: object) -> bool:
        return artifact_id in self.__by_id

    def __len__(self) -> int:
        return len(self.__by_id)

"""In-memory artifact index maintained by the workflow engine.

The index covers both completed artifacts (promoted to the project tree and
mirrored) and in-flight artifacts (living in the workspace).  It is rebuilt
on every cold start from on-disk state by :class:`ProjectBootstrap`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from kodo.workspace._models import ArtifactType

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
        location: Absolute path to the artifact file on disk.
        filename_hint: Leaf filename (stable across superseding revisions).
        supersedes: Prior artifact IDs replaced by this one.
        requirement_ids: Requirements covered by this artifact.
        session_id: Session that produced this artifact (set for in-flight).
        author: Sub-agent name that published the artifact.
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
    last_modified: datetime


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

    def __len__(self) -> int:
        return len(self.__by_id)

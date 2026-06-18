"""Artifact → JSON-serialisable dict helper shared by read tools."""

from __future__ import annotations

from kodo.workspace import Artifact

__all__ = ["serialize_artifact"]


def serialize_artifact(artifact: Artifact) -> dict[str, object]:
    """Convert an :class:`~kodo.workspace.Artifact` to a plain dict for the LLM.

    Args:
        artifact: The artifact to serialise.

    Returns:
        dict[str, object]: JSON-serialisable representation.
    """
    return {
        "id": artifact.id,
        "type": artifact.type.value,
        "author": artifact.author,
        "project_code": artifact.project_code,
        "responsibility_code": artifact.responsibility_code,
        "created_at": artifact.created_at.isoformat(),
        "content": artifact.content,
        "requirement_ids": artifact.requirement_ids,
        "filename_hint": artifact.filename_hint,
        "supersedes": artifact.supersedes,
        "reviewed_artifact_id": artifact.reviewed_artifact_id,
        "verdict": artifact.verdict.value if artifact.verdict else None,
        "concerns": [
            {
                "kind": c.kind,
                "description": c.description,
                "first_line": c.first_line,
                "last_line": c.last_line,
                "excerpt": c.excerpt,
            }
            for c in artifact.concerns
        ],
        "metadata": artifact.metadata,
        "session_id": artifact.session_id,
    }

"""``list_artifacts`` tool spec — orchestrator tool (FR-ORCH-03).

Dispatch lives in :class:`~kodo.runtime._tool_surface.ToolSurface`.
"""

from __future__ import annotations

from kodo.workspace._models import ArtifactType

from ._spec import ToolSpec

__all__ = ["LIST_ARTIFACTS"]


LIST_ARTIFACTS: ToolSpec = ToolSpec(
    name="list_artifacts",
    external_name="List Artifacts",
    user_description="List workspace artifacts",
    description=(
        "Query the workspace index.  All supplied filters are combined with AND. "
        "At least one filter is required."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "Exact artifact UUID."},
            "type": {
                "type": "string",
                "enum": [t.value for t in ArtifactType],
                "description": "Artifact type filter.",
            },
            "responsibility_code": {"type": "string", "description": "Responsibility codename."},
            "requirement_id": {
                "type": "string",
                "description": "Requirement ID that must be in requirement_ids.",
            },
            "author": {
                "type": "string",
                "description": "Sub-agent name that published the artifact.",
            },
            "state": {
                "type": "string",
                "enum": ["completed", "in_flight"],
                "description": "Lifecycle state filter.",
            },
        },
        "required": [],
        "minProperties": 1,
    },
    when_to_use=(
        "A broader inventory view than `query_frontier` provides is needed "
        "— e.g., to enumerate all artifacts for a codename, find superseded "
        "versions, or audit workspace state while diagnosing a "
        "non-converging loop.",
    ),
)

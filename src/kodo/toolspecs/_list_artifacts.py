"""``list_artifacts`` tool spec — orchestrator tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from kodo.workspace import ArtifactType

from ._spec import SecurityImpact, ToolSpec

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
    output_schema={
        "type": "object",
        "properties": {
            "artifacts": {
                "type": "array",
                "description": "Matching artifact metadata entries.",
                "items": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "type": {"type": "string"},
                        "responsibility_code": {"type": "string"},
                        "filename_hint": {"type": ["string", "null"]},
                        "state": {"type": "string"},
                        "author": {"type": "string"},
                    },
                    "required": ["artifact_id", "type"],
                },
            },
        },
        "required": ["artifacts"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={
        "artifact_id": "always",
        "type": "visible",
        "responsibility_code": "visible",
        "requirement_id": "visible",
        "author": "visible",
        "state": "visible",
    },
    output_visibility={"artifacts": "always"},
    when_to_use=(
        "A broader inventory view than `query_frontier` provides is needed "
        "— e.g., to enumerate all artifacts for a codename, find superseded "
        "versions, or audit workspace state while diagnosing a "
        "non-converging loop.",
    ),
)

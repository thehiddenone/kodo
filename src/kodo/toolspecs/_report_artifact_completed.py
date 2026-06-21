"""``report_artifact_completed`` tool spec — leaf sub-agent report tool.

A critic or solo agent marks one artifact as having passed all its gates;
this drives promotion and ``query_frontier``.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["REPORT_ARTIFACT_COMPLETED"]


REPORT_ARTIFACT_COMPLETED: ToolSpec = ToolSpec(
    name="report_artifact_completed",
    external_name="Report Artifact Complete",
    user_description="Mark an artifact complete",
    description=(
        "Mark one artifact as having passed all of its gates — critic "
        "acceptance and, in interactive mode, user review — so it is good to "
        "go. This is the authoritative completion signal: the engine promotes "
        "the artifact and query_frontier reports it completed. Reported per "
        "artifact; call once per completed artifact and never before every gate "
        "condition for it has been met. Held by critics and solo agents."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Workspace ID of the artifact that has passed all its gates.",
            },
        },
        "required": ["artifact_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'completed'."},
            "artifact_id": {"type": "string", "description": "The completed artifact's ID."},
        },
        "required": ["status", "artifact_id"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"artifact_id": "always"},
    output_visibility={"status": "always", "artifact_id": "always"},
    when_to_use=(
        "Immediately after a review's verdict is `accepted` and, in "
        "interactive mode, the user has accepted the artifact via "
        "`request_user_review_artifact`.",
        "Once an artifact has cleared its review gate — one call per "
        "artifact, never bundling multiple artifacts into a single call.",
        "Never before every gate condition for that artifact has been met; "
        "publishing an artifact does not make it complete.",
    ),
)

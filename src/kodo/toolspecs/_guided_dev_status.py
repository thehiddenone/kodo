"""``guided_dev_status`` tool spec — scans tracked documents' status.

Replaces the old artifact-index-based ``query_frontier``. Guided-mode only —
the dispatch handler errors if called from any other workflow mode.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["GUIDED_DEV_STATUS"]


GUIDED_DEV_STATUS: ToolSpec = ToolSpec(
    name="guided_dev_status",
    external_name="Guided Development Status",
    user_description="Check tracked documents' status",
    description=(
        "Scan every tracked document under specs/, src/, and test/ and report "
        "its current status, derived from the last entry of its evolution "
        "log: `pending_review` (just written/revised, not yet reviewed), "
        "`needs_revision` (rejected by a critic or the user), "
        "`pending_acceptance` (critic accepted, awaiting the engine's "
        "acceptance step), or `accepted` (done). A document absent from the "
        "result has never been written. Only available in Guided mode."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    output_schema={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "One entry per tracked document.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending_review",
                                "needs_revision",
                                "pending_acceptance",
                                "accepted",
                            ],
                        },
                        "last_event": {
                            "type": "string",
                            "description": "ISO-8601 timestamp of the last entry.",
                        },
                    },
                    "required": ["path", "status", "last_event"],
                },
            },
        },
        "required": ["files"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    output_visibility={"files": "always"},
    when_to_use=(
        "Before every scheduling decision — the first step of the core loop, every time, "
        "including after invalidation cascades or when pre-existing documents are brought "
        "into the project.",
        "To determine the furthest stage each codename can advance to, to discover documents "
        "still pending review/revision, and to confirm an invalidation cascade correctly "
        "marked downstream documents as needing rework.",
    ),
    requires_project=True,
)

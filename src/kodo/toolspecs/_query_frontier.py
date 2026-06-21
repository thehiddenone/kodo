"""``query_frontier`` tool spec — orchestrator tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["QUERY_FRONTIER"]


QUERY_FRONTIER: ToolSpec = ToolSpec(
    name="query_frontier",
    external_name="Review Workspace",
    user_description="Check the workspace frontier",
    description=(
        "Query the most recent status of every artifact and return the "
        "per-responsibility frontier: for each responsibility_code, the "
        "earliest artifact type in the canonical execution order "
        "(functional-design → test-plan → test → code) that has zero completed "
        "entries.  A responsibility absent from the result has all four types "
        "completed.  An artifact counts as completed only once an agent marks it "
        "so via report_artifact_completed; this tool never decides completion."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    output_schema={
        "type": "object",
        "properties": {
            "frontier": {
                "type": "array",
                "description": "Earliest missing artifact type per responsibility.",
                "items": {
                    "type": "object",
                    "properties": {
                        "responsibility_code": {"type": "string"},
                        "next_type": {"type": "string"},
                    },
                    "required": ["responsibility_code", "next_type"],
                },
            },
        },
        "required": ["frontier"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    output_visibility={"frontier": "always"},
    when_to_use=(
        "Before every scheduling decision — the first step of the core "
        "loop, every time, including after invalidation cascades or when "
        "pre-existing artifacts are brought into the workspace.",
        "To determine the furthest stage each codename can advance to, to "
        "discover artifacts still in flight, and to confirm that an "
        "invalidation cascade has correctly marked downstream artifacts as "
        "missing.",
    ),
)

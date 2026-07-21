"""``finalize_project`` tool spec — guide tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["FINALIZE_PROJECT"]


FINALIZE_PROJECT: ToolSpec = ToolSpec(
    name="finalize_project",
    external_name="Finalize Project",
    user_description="Mark the project as done",
    description=(
        "Terminal call: the project is complete.  "
        "Transitions state.phase to 'done' and ends the Guide session."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'done'."},
        },
        "required": ["status"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={},
    output_visibility={"status": "always"},
    when_to_use=(
        "All product-level stages have completed and the workspace has "
        "nothing left in flight — the project is done.",
    ),
    requires_project=True,
)

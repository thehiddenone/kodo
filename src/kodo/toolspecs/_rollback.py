"""``rollback`` tool spec — guide tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["ROLLBACK"]


ROLLBACK: ToolSpec = ToolSpec(
    name="rollback",
    external_name="Rollback Project",
    user_description="Roll back to a checkpoint",
    description=(
        "Invoke the rollback procedure.  "
        "Restores src/ and gen/ from the target mirror commit, clears the workspace, "
        "and starts a fresh Guide session.  "
        "In interactive mode the Guide MUST confirm with the user via ask_user "
        "before calling this.  In autonomous mode it decides and documents via post_update; "
        "there is no user to confirm with."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_sha": {
                "type": "string",
                "description": "Mirror commit SHA to roll back to.",
            },
        },
        "required": ["target_sha"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'completed' on success."},
        },
        "required": ["status"],
    },
    security_impact=SecurityImpact.HIGH,
    input_visibility={"target_sha": "always"},
    output_visibility={"status": "always"},
    when_to_use=(
        "Rework-in-place would be worse than starting a stage over — "
        "typically after a root-cause resolution invalidates a large "
        "frontier and a checkpoint predates the contaminated work.",
    ),
)

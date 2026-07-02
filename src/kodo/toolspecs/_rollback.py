"""``rollback`` tool spec — guide tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["ROLLBACK"]


ROLLBACK: ToolSpec = ToolSpec(
    name="rollback",
    external_name="Rollback Project",
    user_description="Roll back to a checkpoint",
    description=(
        "Invoke the rollback procedure.  "
        "Moves the project's checkpoint mirror back to the target commit, restoring "
        "specs/, src/, and test/ to that point in history, and resets the session.  "
        "In interactive mode the Guide MUST confirm with the user via ask_user "
        "before calling this.  In autonomous mode it decides and documents via a "
        "<kodo_info> callout; there is no user to confirm with."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "target_sha": {
                "type": "string",
                "description": "Mirror commit SHA to roll back to.",
            },
        },
        "required": ["intent", "target_sha"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'completed' on success."},
        },
        "required": ["status"],
    },
    security_impact=SecurityImpact.HIGH,
    input_visibility={"intent": "always", "target_sha": "always"},
    output_visibility={"status": "always"},
    when_to_use=(
        "Rework-in-place would be worse than starting a stage over — "
        "typically after a root-cause resolution invalidates a large "
        "frontier and a checkpoint predates the contaminated work.",
    ),
)

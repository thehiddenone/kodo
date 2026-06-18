"""``finalize_project`` tool spec — orchestrator tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["FINALIZE_PROJECT"]


FINALIZE_PROJECT: ToolSpec = ToolSpec(
    name="finalize_project",
    external_name="Finalize Project",
    user_description="Mark the project as done",
    description=(
        "Terminal call: the project is complete.  "
        "Transitions state.phase to 'done' and ends the Orchestrator session."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    when_to_use=(
        "All product-level stages have completed and the workspace has "
        "nothing left in flight — the project is done.",
    ),
)

"""``run_subagent`` tool spec — orchestrator tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["RUN_SUBAGENT"]


RUN_SUBAGENT: ToolSpec = ToolSpec(
    name="run_subagent",
    external_name="Run Sub-Agent",
    user_description="Run a solo sub-agent",
    description=(
        "Invoke a leaf sub-agent by name.  Blocks until the sub-agent session "
        "completes.  Returns the artifact IDs the sub-agent published."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Sub-agent name from the registry (e.g. 'narrative_author').",
            },
            "task_message": {
                "type": "string",
                "description": "Task message injected as the initial uncached user turn.",
            },
            "input_artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Artifact IDs the sub-agent may read via read_artifact.",
            },
        },
        "required": ["name", "task_message"],
    },
    when_to_use=(
        "Kicking off a solo agent's stage that doesn't participate in an "
        "author/critic loop, to produce an initial set of artifacts.",
        "Invoking a solo stage that produces artifacts from an "
        "already-accepted upstream artifact (e.g., generating stubs and "
        "tests from an accepted test plan).",
    ),
)

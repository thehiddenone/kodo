"""``run_subagent`` tool spec — guide tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["RUN_SUBAGENT"]


RUN_SUBAGENT: ToolSpec = ToolSpec(
    name="run_subagent",
    external_name="Run Sub-Agent",
    user_description="Run a solo sub-agent",
    description=(
        "Invoke a leaf sub-agent by name.  Blocks until the sub-agent session "
        "completes.  `task_input` is a structured object that MUST conform to the "
        "named sub-agent's input schema (see its entry in `## Subagents`); the "
        "engine validates it.  Returns the structured result the sub-agent "
        "produced via `return_result` (its output schema)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Sub-agent name from the registry (e.g. 'narrative_author').",
            },
            "task_input": {
                "type": "object",
                "description": (
                    "Structured task for the sub-agent, conforming to that agent's input "
                    "schema (typically: instructions, input_artifact_ids, and any agent-"
                    "specific fields)."
                ),
            },
        },
        "required": ["name", "task_input"],
    },
    output_schema={
        "type": "object",
        "description": "The sub-agent's structured result (its declared output schema).",
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={
        "name": "always",
        "task_input": "visible",
    },
    # The result is the sub-agent's own dynamic output schema, so there are no
    # fixed output properties to assign per-key visibility to.
    output_visibility={},
    when_to_use=(
        "Kicking off a solo agent's stage that doesn't participate in an "
        "author/critic loop, to produce an initial set of artifacts.",
        "Invoking a solo stage that produces artifacts from an "
        "already-accepted upstream artifact (e.g., generating stubs and "
        "tests from an accepted test plan).",
    ),
)

"""``run_author_critic_iteration`` tool spec — orchestrator tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["RUN_AUTHOR_CRITIC_ITERATION"]


RUN_AUTHOR_CRITIC_ITERATION: ToolSpec = ToolSpec(
    name="run_author_critic_iteration",
    external_name="Run Author/Critic Round",
    user_description="Run one author/critic round",
    description=(
        "Execute one round of the Author/Critic loop.  "
        "Spawns the Author (with previous_artifact_id as feedback context when provided), "
        "then spawns the Critic against the Author's output.  "
        "Returns the artifact ID, verdict, and concerns.  "
        "Call again to iterate; the Orchestrator decides when to stop."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_name": {"type": "string", "description": "Author sub-agent name."},
            "critic_name": {"type": "string", "description": "Critic sub-agent name."},
            "input_artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Input artifact IDs passed to the Author.",
            },
            "previous_artifact_id": {
                "type": "string",
                "description": (
                    "Artifact ID of the prior Author output.  When set, the Author "
                    "receives it as revision context alongside the Critic's concerns."
                ),
            },
        },
        "required": ["author_name", "critic_name", "input_artifact_ids"],
    },
    when_to_use=(
        "Any stage with an author/critic pairing, to run one author→critic round.",
        "Called repeatedly within a per-loop iteration budget (a sensible "
        "default is up to 5 rounds), stopping early when findings converge "
        "or when findings stop decreasing (treating the latter as "
        "non-convergence).",
    ),
)

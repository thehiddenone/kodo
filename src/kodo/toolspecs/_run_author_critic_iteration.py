"""``run_author_critic_iteration`` tool spec — guide tool (FR-ORCH-03).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

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
        "Call again to iterate; the Guide decides when to stop."
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
            "for_revision_artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Artifact IDs of the prior Author outputs to revise.  When non-empty, "
                    "the Author receives them as revision context alongside the Critic's "
                    "concerns.  A list because an Author may have published several "
                    "artifacts that all need revision."
                ),
            },
        },
        "required": ["author_name", "critic_name", "input_artifact_ids"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": ["string", "null"],
                "description": "The author's published artifact ID (null if none).",
            },
            "verdict": {"type": "string", "description": "Critic verdict (accepted/rejected)."},
            "concerns": {
                "type": "array",
                "description": "Concerns raised by the critic.",
                "items": {"type": "object"},
            },
        },
        "required": ["artifact_id", "verdict", "concerns"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "author_name": "always",
        "critic_name": "always",
        "input_artifact_ids": "visible",
        "for_revision_artifact_ids": "visible",
    },
    output_visibility={"artifact_id": "always", "verdict": "always", "concerns": "visible"},
    when_to_use=(
        "Any stage with an author/critic pairing, to run one author→critic round.",
        "Called repeatedly within a per-loop iteration budget (a sensible "
        "default is up to 5 rounds), stopping early when findings converge "
        "or when findings stop decreasing (treating the latter as "
        "non-convergence).",
    ),
)

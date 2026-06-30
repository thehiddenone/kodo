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
        "Execute one round of the Author/Critic loop over a real file.  "
        "Spawns the Author (passing `for_revision: true` and `path` when revising a "
        "prior round — omit `path` on a fresh round, since the Author chooses it), "
        "then spawns the Critic against the Author's reported primary file.  "
        "Returns the file's path, status, and concerns.  "
        "Call again to iterate; the Guide decides when to stop."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_name": {"type": "string", "description": "Author sub-agent name."},
            "critic_name": {"type": "string", "description": "Critic sub-agent name."},
            "path": {
                "type": "string",
                "description": (
                    "The file to revise. Required when `for_revision` is true; omit on a "
                    "fresh round — the Author chooses the path and reports it back."
                ),
            },
            "input_paths": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Named collection (label -> path) of context files for the Author to "
                    'read this round, e.g. {"requirements": "specs/requirements.md"}.'
                ),
            },
            "instructions": {
                "type": "string",
                "description": "What the Author should do this round.",
            },
            "for_revision": {
                "type": "boolean",
                "description": "True when `path` already exists and this round revises it.",
            },
        },
        "required": ["author_name", "critic_name", "instructions"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The author's primary file path.",
            },
            "status": {
                "type": "string",
                "description": "Status derived from the file's evolution log after this round.",
            },
            "concerns": {
                "type": "array",
                "description": "Concerns raised by the critic.",
                "items": {"type": "object"},
            },
        },
        "required": ["path", "status", "concerns"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "author_name": "always",
        "critic_name": "always",
        "path": "always",
        "input_paths": "visible",
        "instructions": "visible",
        "for_revision": "visible",
    },
    output_visibility={"path": "always", "status": "always", "concerns": "visible"},
    when_to_use=(
        "Any stage with an author/critic pairing, to run one author→critic round.",
        "Called repeatedly within a per-loop iteration budget (a sensible "
        "default is up to 5 rounds), stopping early when findings converge "
        "or when findings stop decreasing (treating the latter as "
        "non-convergence).",
    ),
)

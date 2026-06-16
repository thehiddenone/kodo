"""``ask_user`` tool spec — orchestrator variant (FR-ORCH-03).

Dispatch lives in :class:`~kodo.runtime._tool_surface.ToolSurface`.

This is a distinct :class:`ToolSpec` from the leaf sub-agent's ``ask_user``
(see :data:`kodo.toolspecs._ask_user.ASK_USER`) — same tool name, but the
description and schema are tailored to the Orchestrator's role.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["ORCHESTRATOR_ASK_USER"]


ORCHESTRATOR_ASK_USER: ToolSpec = ToolSpec(
    name="ask_user",
    external_name="Ask User",
    user_description="Ask the user a question",
    description=(
        "Surface a free-form or choice question to the user. "
        "Blocks until the user responds. "
        "Use for clarification, confirmation before destructive operations, and intake."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to display."},
            "mode": {
                "type": "string",
                "enum": ["free_text", "choice"],
                "description": "free_text: user types a reply; choice: user picks from choices.",
            },
            "choices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                    },
                    "required": ["key", "label"],
                },
                "description": "Required when mode='choice'.",
            },
        },
        "required": ["question"],
    },
    when_to_use=(
        "Eliciting the single most important uncovered or partially-covered "
        "piece of information during intake or gap-filling, or resolving a "
        "contradiction in supplied input before incorporating it — one "
        "concern per call.",
        "Triaging a substantive escalation — a judgment call about what the "
        "product should do, or which of two conflicting positions is "
        "correct.",
        "Resolving an ambiguous rework target — an upstream contradiction "
        "that could be fixed on either side.",
        "Confirming a rollback, or confirming a large invalidation cascade "
        "(more than one codename's worth of downstream artifacts), before "
        "executing it.",
        "Presenting a root-cause diagnosis after pipeline-level cycle "
        "detection fires (alongside `disable_autonomous_mode`).",
    ),
    autonomous_mode=(
        "unavailable — there is no user to consult while the user is away, "
        "so this tool is withheld entirely. Decide and document the choice "
        "via `post_update` instead; if non-convergence is detected, pull the "
        "break-glass via `disable_autonomous_mode`."
    ),
)

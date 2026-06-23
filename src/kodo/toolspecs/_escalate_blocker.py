"""``escalate_blocker`` tool spec — leaf sub-agent report tool.

An author hands a blocker it cannot resolve to the guide, which
triages it (and may surface it to the user).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["ESCALATE_BLOCKER"]


ESCALATE_BLOCKER: ToolSpec = ToolSpec(
    name="escalate_blocker",
    external_name="Escalate Blocker",
    user_description="Escalate a blocker to the guide",
    description=(
        "Hand a blocking issue the agent cannot defensibly resolve to the "
        "guide. Use when an iteration cap is exhausted, when a "
        "back-and-forth between two sub-agents cannot be reconciled, or when "
        "input artifacts are insufficient. The guide owns the "
        "resolution: it triages procedurally, decides itself in autonomous "
        "mode, or surfaces the matter to the user via ask_user in interactive "
        "mode. The resolution arrives as the agent's next input. For an input "
        "or clarification the agent can act on itself, use ask_user instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Short identifier of why escalation is needed "
                    "(e.g. 'critic_iteration_cap', "
                    "'unreconciled_routing', "
                    "'missing_tech_stack_field')."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Plain-English summary of the current state of "
                    "the work and what is blocking it."
                ),
            },
            "blocking_artifact_ids": {
                "type": "array",
                "description": (
                    "IDs of workspace artifacts the guide (or user) "
                    "must inspect to adjudicate: the artifact under review, "
                    "the feedback artifacts in dispute, and any neighbouring "
                    "artifacts that bear on the decision. Empty array when the "
                    "blocker is missing input rather than disputed content."
                ),
                "items": {"type": "string"},
            },
            "options": {
                "type": "array",
                "description": (
                    "Concrete options to choose between when the escalation "
                    "admits discrete alternatives. Empty array if free "
                    "direction is being requested."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["reason", "summary"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'escalated'."},
            "reason": {"type": "string", "description": "Echoed escalation reason."},
            "user_response": {
                "type": "string",
                "description": "User's reply (interactive mode only).",
            },
        },
        "required": ["status", "reason"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "reason": "always",
        "summary": "visible",
        "blocking_artifact_ids": "visible",
        "options": "visible",
    },
    output_visibility={"status": "always", "reason": "always", "user_response": "visible"},
    when_to_use=(
        "Inputs are too under-specified to make a defensible call — there "
        "is no reasonable basis for a required decision, an unambiguous "
        "requirement cannot be written, or a behavioral test cannot be "
        "derived.",
        "An author/critic or reviewer loop ends without convergence and the "
        'critic is still rejecting (`reason: "critic_iteration_cap"` / '
        '`"reviewer_iteration_cap"`).',
        "User feedback at a review gate contradicts upstream artifacts or "
        "itself in a way that cannot be resolved "
        '(`reason: "feedback_contradiction"`).',
        "A red/green implementation loop stops converging "
        '(`reason: "test_iteration_cap"`), or an exchange between two '
        'agents ends without agreement (`reason: "test_coder_disagreement"`).',
        "Validation against a dependency graph fails "
        '(`reason: "dag_validation_failed"`), or a reopen cascade exceeds '
        'its bound (`reason: "reopen_cascade"`).',
    ),
)

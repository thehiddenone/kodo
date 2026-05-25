"""Schema-only tool specs for user escalation and Narrative Author dialog.

This module defines the four tools that have no mapping to the
workspace ``publish_artifact`` / ``read_artifact`` model:

* :data:`ESCALATE_TO_USER` — every author calls this when an iteration
  cap is reached or inputs are insufficient and the user must
  adjudicate.
* :data:`NARRATIVE_ASK_USER_QUESTION`,
  :data:`NARRATIVE_PRESENT_FOR_ACCEPTANCE`,
  :data:`NARRATIVE_REPORT_COMPLETED` — used only by Narrative Author,
  the sole sub-agent that interacts with the user mid-stream.

Artifact production, revision, cross-agent routing, and critic
feedback all happen through ``publish_artifact`` and ``read_artifact``
in the workspace MCP server (authoritative schemas live in
``E:/source/kodo/schemas/``). Sub-agent prompts name these tools by
their ``name`` attribute and never restate the schema.
"""

from __future__ import annotations

from kodo.llms import ToolSpec

__all__ = [
    "ESCALATE_TO_USER",
    "NARRATIVE_ASK_USER_QUESTION",
    "NARRATIVE_PRESENT_FOR_ACCEPTANCE",
    "NARRATIVE_REPORT_COMPLETED",
    "REPORT_TOOLS_BY_NAME",
]


ESCALATE_TO_USER: ToolSpec = ToolSpec(
    name="escalate_to_user",
    description=(
        "Called by a sub-agent when an iteration cap is exhausted, "
        "when a back-and-forth between two sub-agents cannot be "
        "reconciled, or when input artifacts are insufficient and the "
        "user is the only authority who can resolve. The engine "
        "surfaces the escalation through the approval gate machinery."
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
                    "IDs of workspace artifacts the user must inspect "
                    "to adjudicate: the artifact under review, the "
                    "feedback artifacts in dispute, and any "
                    "neighbouring artifacts that bear on the "
                    "decision. Empty array when the blocker is "
                    "missing input rather than disputed content."
                ),
                "items": {"type": "string"},
            },
            "options": {
                "type": "array",
                "description": (
                    "Concrete options the user can choose between "
                    "when the escalation admits discrete "
                    "alternatives. Empty array if the user is being "
                    "asked to provide free direction."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["reason", "summary"],
    },
)


NARRATIVE_ASK_USER_QUESTION: ToolSpec = ToolSpec(
    name="narrative_ask_user_question",
    description=(
        "Called by Narrative Author to ask the user a single focused "
        "clarifying question. The engine sends the question to the "
        "user and feeds the user's reply back as the next input. "
        "Exactly one question per call — bundling multiple questions "
        "is not permitted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The exact question to present to the user. "
                    "Single focused question, plain language, no "
                    "bundled sub-questions."
                ),
            },
            "phase": {
                "type": "string",
                "enum": ["narrative", "tech_stack"],
                "description": ("Which phase of Narrative Author the question belongs to."),
            },
            "covers_points": {
                "type": "array",
                "description": (
                    "List of Required Understanding point names this "
                    "question targets. For the Narrative phase, drawn "
                    "from {'customer', 'problem', 'primary_function', "
                    "'integrations', 'deployment_model', "
                    "'operations', 'north_star'}. For the Tech Stack "
                    "phase, the field name being resolved."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["question", "phase"],
    },
)


NARRATIVE_PRESENT_FOR_ACCEPTANCE: ToolSpec = ToolSpec(
    name="narrative_present_for_acceptance",
    description=(
        "Called by Narrative Author to present a published workspace "
        "artifact (Narrative or Tech Stack) to the user for "
        "accept/feedback. The artifact must already be published via "
        "publish_artifact. The engine relays the user's response back "
        "as the next input."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_kind": {
                "type": "string",
                "enum": ["narrative", "tech_stack"],
                "description": ("Kind of artifact being presented for acceptance."),
            },
            "artifact_id": {
                "type": "string",
                "description": (
                    "Workspace ID of the artifact, as returned by the "
                    "preceding publish_artifact call."
                ),
            },
        },
        "required": ["artifact_kind", "artifact_id"],
    },
)


NARRATIVE_REPORT_COMPLETED: ToolSpec = ToolSpec(
    name="narrative_report_completed",
    description=(
        "Called by Narrative Author exactly once, after both the "
        "Narrative and the Tech Stack have been accepted by the user. "
        "Signals that the entire Narrative Author run is finished."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "narrative_artifact_id": {
                "type": "string",
                "description": ("Workspace ID of the accepted Narrative artifact."),
            },
            "tech_stack_artifact_id": {
                "type": "string",
                "description": ("Workspace ID of the accepted Tech Stack artifact."),
            },
        },
        "required": [
            "narrative_artifact_id",
            "tech_stack_artifact_id",
        ],
    },
)


REPORT_TOOLS_BY_NAME: dict[str, ToolSpec] = {
    ESCALATE_TO_USER.name: ESCALATE_TO_USER,
    NARRATIVE_ASK_USER_QUESTION.name: NARRATIVE_ASK_USER_QUESTION,
    NARRATIVE_PRESENT_FOR_ACCEPTANCE.name: NARRATIVE_PRESENT_FOR_ACCEPTANCE,
    NARRATIVE_REPORT_COMPLETED.name: NARRATIVE_REPORT_COMPLETED,
}

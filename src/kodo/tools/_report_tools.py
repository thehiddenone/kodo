"""Schema-only tool specs for sub-agent user-interaction and completion.

This module defines the tools that have no mapping to the workspace
``publish_artifact`` / ``read_artifact`` model:

* :data:`ESCALATE_BLOCKER` — an author hands a blocker it cannot resolve to
  the orchestrator, which triages it (and may surface it to the user).
* :data:`ASK_USER` — an agent asks the user one focused question and acts on
  the answer itself. Withheld in autonomous mode (no answer to synthesize).
* :data:`REQUEST_USER_REVIEW_ARTIFACT` — a critic or solo agent presents a
  converged artifact for the user's accept/feedback. Auto-accepted in
  autonomous mode.
* :data:`REPORT_ARTIFACT_COMPLETED` — a critic or solo agent marks one artifact
  as having passed all its gates; this drives promotion and ``query_frontier``.

Artifact production, revision, cross-agent routing, and critic feedback all
happen through ``publish_artifact`` and ``read_artifact``, dispatched in-process
by :class:`~kodo.runtime._subagent_dispatch.SubagentDispatcher` (authoritative
schemas live in ``schemas/``). Sub-agent prompts name these tools by their
``name`` attribute and never restate the schema.
"""

from __future__ import annotations

from kodo.llms import ToolSpec

__all__ = [
    "ASK_USER",
    "ESCALATE_BLOCKER",
    "REPORT_ARTIFACT_COMPLETED",
    "REPORT_TOOLS_BY_NAME",
    "REQUEST_USER_REVIEW_ARTIFACT",
]


ESCALATE_BLOCKER: ToolSpec = ToolSpec(
    name="escalate_blocker",
    description=(
        "Hand a blocking issue the agent cannot defensibly resolve to the "
        "orchestrator. Use when an iteration cap is exhausted, when a "
        "back-and-forth between two sub-agents cannot be reconciled, or when "
        "input artifacts are insufficient. The orchestrator owns the "
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
                    "IDs of workspace artifacts the orchestrator (or user) "
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
)


ASK_USER: ToolSpec = ToolSpec(
    name="ask_user",
    description=(
        "Ask the user a single focused question and block until they respond. "
        "The user is the source of information the agent needs and can then act "
        "on itself; the agent keeps ownership of its task. Exactly one question "
        "per call — bundling is not permitted. Unavailable in autonomous mode "
        "(there is no answer to synthesize when the user is away); assume and "
        "document, or escalate_blocker, instead. Distinct from "
        "request_user_review_artifact, which is a sign-off on a finished "
        "artifact rather than a question."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The exact question to present to the user. Single focused "
                    "question, plain language, no bundled sub-questions."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["free_text", "choice"],
                "description": (
                    "free_text: the user types a reply; choice: the user picks "
                    "from the supplied choices. Defaults to free_text."
                ),
            },
            "choices": {
                "type": "array",
                "description": (
                    "Choices to present when mode='choice'; list of "
                    "{'key': str, 'label': str} objects."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                    },
                    "required": ["key", "label"],
                },
            },
        },
        "required": ["question"],
    },
)


REQUEST_USER_REVIEW_ARTIFACT: ToolSpec = ToolSpec(
    name="request_user_review_artifact",
    description=(
        "Present a converged, just-published artifact to the user for "
        "accept/feedback by its artifact_id. The user acts as critic, judging "
        "a finished artifact. Blocks until the user responds; accept ends the "
        "review gate, feedback opens a revision round. Held by critics and solo "
        "agents — the agent that owns the convergence verdict. In autonomous "
        "mode the engine auto-accepts and returns immediately, so call it "
        "unconditionally without branching on mode."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": (
                    "Workspace ID of the artifact to present, as returned by "
                    "the preceding publish_artifact call."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary shown to the user with the artifact.",
            },
        },
        "required": ["artifact_id"],
    },
)


REPORT_ARTIFACT_COMPLETED: ToolSpec = ToolSpec(
    name="report_artifact_completed",
    description=(
        "Mark one artifact as having passed all of its gates — critic "
        "acceptance and, in interactive mode, user review — so it is good to "
        "go. This is the authoritative completion signal: the engine promotes "
        "the artifact and query_frontier reports it completed. Reported per "
        "artifact; call once per completed artifact and never before every gate "
        "condition for it has been met. Held by critics and solo agents."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Workspace ID of the artifact that has passed all its gates.",
            },
        },
        "required": ["artifact_id"],
    },
)


REPORT_TOOLS_BY_NAME: dict[str, ToolSpec] = {
    ESCALATE_BLOCKER.name: ESCALATE_BLOCKER,
    ASK_USER.name: ASK_USER,
    REQUEST_USER_REVIEW_ARTIFACT.name: REQUEST_USER_REVIEW_ARTIFACT,
    REPORT_ARTIFACT_COMPLETED.name: REPORT_ARTIFACT_COMPLETED,
}

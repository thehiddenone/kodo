"""``ask_user`` tool spec — leaf sub-agent report tool.

An agent asks the user one focused question and acts on the answer itself.
Withheld in autonomous mode (no answer to synthesize).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["ASK_USER"]


ASK_USER: ToolSpec = ToolSpec(
    name="ask_user",
    external_name="Ask User",
    user_description="Ask the user a question",
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
    output_schema={
        "type": "object",
        "properties": {
            "answer_text": {"type": "string", "description": "Free-text answer (free_text mode)."},
            "choice_key": {"type": "string", "description": "Selected choice key (choice mode)."},
        },
        "required": [],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"question": "always", "mode": "visible", "choices": "visible"},
    output_visibility={"answer_text": "always", "choice_key": "always"},
    when_to_use=(
        "Eliciting the single most important uncovered or partially-covered "
        "piece of information during gap-filling, or resolving a "
        "contradiction in user-supplied input before incorporating it — one "
        "concern per call.",
    ),
    autonomous_mode=(
        "Unavailable — there is no answer to synthesize when the user is "
        "away, so this tool is withheld entirely. An agent that would have "
        "asked must instead assume-and-document or, if blocked, "
        "`escalate_blocker`."
    ),
)

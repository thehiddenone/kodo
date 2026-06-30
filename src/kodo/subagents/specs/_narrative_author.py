"""SubAgentSpec for ``narrative_author`` (stage 1, solo, user-facing)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import pipeline_input

__all__ = ["NARRATIVE_AUTHOR"]


NARRATIVE_AUTHOR: SubAgentSpec = SubAgentSpec(
    name="narrative_author",
    description="Produces the Narrative and Tech Stack documents from a dialogue with the user.",
    input_schema=pipeline_input(
        input_paths=(
            "Optional: the agent's own prior Narrative/Tech Stack documents when handling "
            "feedback. Works primarily from the user prompt and attachments, so inputs are not "
            "required."
        ),
        require_input_paths=False,
    ),
    output_schema={
        "type": "object",
        "properties": {
            "narrative_path": {
                "type": "string",
                "description": "Path of the Narrative document this round produced or revised.",
            },
            "tech_stack_path": {
                "type": "string",
                "description": "Path of the Tech Stack document this round produced or revised.",
            },
            "project_code": {
                "type": "string",
                "description": "The PROJECTCODE derived for this project.",
            },
            "summary": {
                "type": "string",
                "description": "One line: what was produced or changed.",
            },
        },
        "required": ["narrative_path", "tech_stack_path", "project_code", "summary"],
    },
)

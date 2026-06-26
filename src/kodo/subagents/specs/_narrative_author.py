"""SubAgentSpec for ``narrative_author`` (stage 1, solo, user-facing)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import pipeline_input

__all__ = ["NARRATIVE_AUTHOR"]


NARRATIVE_AUTHOR: SubAgentSpec = SubAgentSpec(
    name="narrative_author",
    description="Produces the Narrative and Tech Stack documents from a dialogue with the user.",
    input_schema=pipeline_input(
        input_artifacts=(
            "Optional: the agent's own prior Narrative/Tech Stack when handling feedback. "
            "Works primarily from the user prompt and attachments, so artifacts are not required."
        ),
        require_input_artifacts=False,
    ),
    output_schema={
        "type": "object",
        "properties": {
            "narrative_artifact_id": {
                "type": "string",
                "description": "ID of the published Narrative artifact (type=narrative).",
            },
            "tech_stack_artifact_id": {
                "type": "string",
                "description": "ID of the published Tech Stack artifact (type=tech-stack).",
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
        "required": ["narrative_artifact_id", "tech_stack_artifact_id", "project_code", "summary"],
    },
)

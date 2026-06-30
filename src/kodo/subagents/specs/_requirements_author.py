"""SubAgentSpec for ``requirements_author`` (stage 3 author)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["REQUIREMENTS_AUTHOR"]


REQUIREMENTS_AUTHOR: SubAgentSpec = SubAgentSpec(
    name="requirements_author",
    description="Writes the structured per-responsibility requirements document.",
    input_schema=pipeline_input(
        input_paths="Must include the architecture and the Narrative (for the North Star).",
    ),
    output_schema=author_output(
        extra_properties={
            "requirement_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "All PROJECTCODE_RESPONSIBILITYCODE_REQUIREMENTCODE IDs produced.",
            },
        },
        extra_required=["requirement_ids"],
    ),
)

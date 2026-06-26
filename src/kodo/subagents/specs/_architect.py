"""SubAgentSpec for ``architect`` (stage 2 author, paired with architect_critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["ARCHITECT"]


ARCHITECT: SubAgentSpec = SubAgentSpec(
    name="architect",
    description=(
        "Decomposes the product into codenamed responsibilities and determines "
        "end-to-end testability."
    ),
    input_schema=pipeline_input(
        input_artifacts=(
            "Must include the accepted Narrative (type=narrative) and Tech Stack (type=tech-stack)."
        ),
    ),
    output_schema=author_output(
        extra_properties={
            "end_to_end_testable": {
                "type": "string",
                "enum": ["applicable", "excluded"],
                "description": (
                    "The Architect's end-to-end testability determination (Part 3 of the "
                    "architecture document). The Guide reads the stage-8 gate from this value."
                ),
            },
        },
        extra_required=["end_to_end_testable"],
    ),
)

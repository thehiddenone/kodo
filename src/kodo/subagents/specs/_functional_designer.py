"""SubAgentSpec for ``functional_designer`` (stage 4 author)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["FUNCTIONAL_DESIGNER"]


FUNCTIONAL_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="functional_designer",
    description="Produces the Design Plan (DAG, direction, order) and per-codename designs.",
    input_schema=pipeline_input(
        input_paths="Must include the architecture, requirements, Narrative, and Tech Stack.",
    ),
    output_schema=author_output(
        extra_properties={
            "component_order": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The Design Plan's component order (codenames in batch order). "
                    "Set when this round produced the Design Plan; omit otherwise."
                ),
            },
        },
    ),
)

"""SubAgentSpec for ``functional_designer`` (stage 4 author)."""

from __future__ import annotations

from .._artifacts import (
    PRODUCES_REMAINDER,
    ROLE_ARCHITECTURE,
    ROLE_DESIGN_PLAN,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_NARRATIVE,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["FUNCTIONAL_DESIGNER"]


FUNCTIONAL_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="functional_designer",
    input_schema=pipeline_input(
        input_paths="Must include the architecture, requirements, Narrative, and Tech Stack.",
    ),
    output_schema=author_output(
        extra_properties={
            "design_plan_path": {
                "type": "string",
                "description": (
                    "Path of the Design Plan document, when this round produced or revised "
                    "it; omit otherwise. Also listed in `paths` like every other file you "
                    "wrote — naming it here as well is what lets the engine tell the Plan "
                    "apart from the Functional Designs when it hands them to later stages."
                ),
            },
            "designs": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Which Functional Design you wrote for which component: "
                    '{"LEADERBOARD": "billing-service/specs/design/LEADERBOARD.md"}. '
                    "Every design in `paths` should appear here. You write them per "
                    "component, so you already know this — declaring it is what lets a "
                    "later stage be handed one component's design rather than all of them."
                ),
            },
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
    produces={ROLE_DESIGN_PLAN: "design_plan_path", ROLE_FUNCTIONAL_DESIGN: PRODUCES_REMAINDER},
    component_paths="designs",
    consumes=(
        Need(ROLE_ARCHITECTURE),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_NARRATIVE),
        Need(ROLE_TECH_STACK),
    ),
)

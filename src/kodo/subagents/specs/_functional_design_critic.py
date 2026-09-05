"""SubAgentSpec for ``functional_design_critic`` (stage 4 critic)."""

from __future__ import annotations

from .._artifacts import (
    ROLE_ARCHITECTURE,
    ROLE_DESIGN_PLAN,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_NARRATIVE,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    SCOPE_UNDER_REVIEW,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["FUNCTIONAL_DESIGN_CRITIC"]


FUNCTIONAL_DESIGN_CRITIC: SubAgentSpec = SubAgentSpec(
    name="functional_design_critic",
    input_schema=pipeline_input(
        input_paths=(
            "The Functional Design under review, the Design Plan, the upstream documents, and "
            "any locked peer Functional Designs for interface-consistency checks."
        ),
    ),
    output_schema=critic_output(),
    produces={},
    consumes=(
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_UNDER_REVIEW),
        Need(ROLE_DESIGN_PLAN),
        Need(ROLE_ARCHITECTURE),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_NARRATIVE),
        Need(ROLE_TECH_STACK),
    ),
)

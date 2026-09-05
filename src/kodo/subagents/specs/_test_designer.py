"""SubAgentSpec for ``test_designer`` (stage 5 author, critic = test_design_critic)."""

from __future__ import annotations

from .._artifacts import (
    PRODUCES_REMAINDER,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    ROLE_TEST_PLAN,
    SCOPE_SELF,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["TEST_DESIGNER"]


TEST_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="test_designer",
    input_schema=pipeline_input(
        input_paths=(
            "Must include this component's Functional Design, the requirements, and the Tech Stack."
        ),
        require_responsibility=True,
    ),
    output_schema=author_output(),
    produces={ROLE_TEST_PLAN: PRODUCES_REMAINDER},
    consumes=(
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_SELF),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_TECH_STACK),
    ),
)

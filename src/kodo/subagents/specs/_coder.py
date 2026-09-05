"""SubAgentSpec for ``coder`` (stage 7 author, paired with code_critic)."""

from __future__ import annotations

from .._artifacts import (
    PRODUCES_REMAINDER,
    ROLE_CODE,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    ROLE_TEST_CODE,
    ROLE_TEST_PLAN,
    SCOPE_DEPENDENCIES,
    SCOPE_SELF,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["CODER"]


CODER: SubAgentSpec = SubAgentSpec(
    name="coder",
    input_schema=pipeline_input(
        input_paths=(
            "This component's Functional Design, requirements, Test Plan, Tech Stack, the "
            "Functional Designs of consumed/consuming components, and the current stub code "
            "(written by test_coder) to supersede. Never the test source or peer code."
        ),
        require_responsibility=True,
    ),
    output_schema=author_output(),
    produces={ROLE_CODE: PRODUCES_REMAINDER},
    consumes=(
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_SELF),
        # An interface has two sides: the designs of the components this one
        # consumes or is consumed by. Optional because a component with no
        # neighbours legitimately has none.
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_DEPENDENCIES, required=False),
        Need(ROLE_TEST_PLAN, SCOPE_SELF),
        # The stub code test_coder wrote for this component, to supersede.
        Need(ROLE_TEST_CODE, SCOPE_SELF),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_TECH_STACK),
    ),
)

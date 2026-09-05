"""SubAgentSpec for ``test_design_critic`` (stage 5 critic of ``test_designer``)."""

from __future__ import annotations

from .._artifacts import (
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    ROLE_TEST_PLAN,
    SCOPE_SELF,
    SCOPE_UNDER_REVIEW,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["TEST_DESIGN_CRITIC"]


TEST_DESIGN_CRITIC: SubAgentSpec = SubAgentSpec(
    name="test_design_critic",
    input_schema=pipeline_input(
        input_paths=(
            "The Test Plan under review, plus this component's Functional Design, the "
            "requirements, and the Tech Stack."
        ),
        # Deliberately NOT require_responsibility: a critic's task is built by
        # the engine (`instructions` + resolved `input_paths`), never by a
        # caller, so it never received this field even while it declared it.
        # Its SCOPE_SELF need is narrowed from the work product under review's
        # own responsibility_code, which the engine already passes to
        # `resolve_needs` — the component is a fact of the round, not a task
        # field for anyone to supply.
    ),
    output_schema=critic_output(),
    produces={},
    consumes=(
        Need(ROLE_TEST_PLAN, SCOPE_UNDER_REVIEW),
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_SELF),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_TECH_STACK),
    ),
)

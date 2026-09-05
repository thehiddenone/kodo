"""SubAgentSpec for ``e2e_test_design_critic`` (stage 8 critic)."""

from __future__ import annotations

from .._artifacts import (
    ROLE_ARCHITECTURE,
    ROLE_DESIGN_PLAN,
    ROLE_E2E_TEST_PLAN,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_NARRATIVE,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    SCOPE_ALL,
    SCOPE_UNDER_REVIEW,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["E2E_TEST_DESIGN_CRITIC"]


E2E_TEST_DESIGN_CRITIC: SubAgentSpec = SubAgentSpec(
    name="e2e_test_design_critic",
    input_schema=pipeline_input(
        input_paths=(
            "The End-to-End Test Plan under review, plus the architecture, requirements, "
            "Narrative, Tech Stack, Design Plan, and all Functional Designs."
        ),
    ),
    output_schema=critic_output(),
    produces={},
    consumes=(
        Need(ROLE_E2E_TEST_PLAN, SCOPE_UNDER_REVIEW),
        Need(ROLE_ARCHITECTURE),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_NARRATIVE),
        Need(ROLE_TECH_STACK),
        Need(ROLE_DESIGN_PLAN),
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_ALL),
    ),
)

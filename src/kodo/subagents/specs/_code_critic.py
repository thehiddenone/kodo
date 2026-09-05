"""SubAgentSpec for ``code_critic`` (stage 7 critic; reviews code and test artifacts)."""

from __future__ import annotations

from .._artifacts import (
    ROLE_CODE,
    ROLE_TECH_STACK,
    SCOPE_UNDER_REVIEW,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["CODE_CRITIC"]


CODE_CRITIC: SubAgentSpec = SubAgentSpec(
    name="code_critic",
    input_schema=pipeline_input(
        input_paths=(
            "The single code or test file under review. Never the design, requirements, or "
            "peer code."
        ),
    ),
    output_schema=critic_output(),
    produces={},
    consumes=(
        Need(ROLE_CODE, SCOPE_UNDER_REVIEW),
        # Language/framework context only: this critic judges code AS code
        # and must not receive the design, requirements, or test plan.
        Need(ROLE_TECH_STACK),
    ),
)

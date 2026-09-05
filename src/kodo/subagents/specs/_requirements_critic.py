"""SubAgentSpec for ``requirements_critic`` (stage 3 critic)."""

from __future__ import annotations

from .._artifacts import (
    ROLE_ARCHITECTURE,
    ROLE_NARRATIVE,
    ROLE_REQUIREMENTS,
    SCOPE_UNDER_REVIEW,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["REQUIREMENTS_CRITIC"]


REQUIREMENTS_CRITIC: SubAgentSpec = SubAgentSpec(
    name="requirements_critic",
    input_schema=pipeline_input(
        input_paths=(
            "The requirements document under review and the architecture for sub-narratives "
            "and decomposition decisions."
        ),
    ),
    output_schema=critic_output(),
    produces={},
    consumes=(
        Need(ROLE_REQUIREMENTS, SCOPE_UNDER_REVIEW),
        # The input that went missing for ~1133 rounds (doc/FINDINGS.md).
        Need(ROLE_ARCHITECTURE),
        Need(ROLE_NARRATIVE),
    ),
)

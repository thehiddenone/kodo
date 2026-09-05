"""SubAgentSpec for ``architect_critic`` (stage 2 critic)."""

from __future__ import annotations

from .._artifacts import (
    ROLE_ARCHITECTURE,
    SCOPE_UNDER_REVIEW,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["ARCHITECT_CRITIC"]


ARCHITECT_CRITIC: SubAgentSpec = SubAgentSpec(
    name="architect_critic",
    input_schema=pipeline_input(
        input_paths="The architecture document under review.",
    ),
    output_schema=critic_output(),
    produces={},
    consumes=(Need(ROLE_ARCHITECTURE, SCOPE_UNDER_REVIEW),),
)

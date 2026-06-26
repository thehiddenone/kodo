"""SubAgentSpec for ``architect_critic`` (stage 2 critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["ARCHITECT_CRITIC"]


ARCHITECT_CRITIC: SubAgentSpec = SubAgentSpec(
    name="architect_critic",
    description="Reviews the architecture document for sound, single-responsibility decomposition.",
    input_schema=pipeline_input(
        input_artifacts="The architecture artifact under review (type=architecture).",
    ),
    output_schema=critic_output(
        [
            "multiple_responsibilities",
            "over_fragmentation",
            "gap",
            "contradiction",
            "orphan",
            "ambiguous_ownership",
        ]
    ),
)

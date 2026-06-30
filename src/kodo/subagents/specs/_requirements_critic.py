"""SubAgentSpec for ``requirements_critic`` (stage 3 critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["REQUIREMENTS_CRITIC"]


REQUIREMENTS_CRITIC: SubAgentSpec = SubAgentSpec(
    name="requirements_critic",
    description="Reviews the requirements for clarity, completeness, and North Star alignment.",
    input_schema=pipeline_input(
        input_paths=(
            "The requirements document under review and the architecture for sub-narratives "
            "and decomposition decisions."
        ),
    ),
    output_schema=critic_output(
        [
            "ambiguity",
            "compound",
            "missing_field",
            "contradiction",
            "uncaptured_assumption",
            "gap",
            "scope_creep",
            "north_star_misalignment",
        ]
    ),
)

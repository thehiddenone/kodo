"""SubAgentSpec for ``coder`` (stage 7 author, paired with code_critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["CODER"]


CODER: SubAgentSpec = SubAgentSpec(
    name="coder",
    description="Writes the production implementation for a component so its tests pass.",
    input_schema=pipeline_input(
        input_paths=(
            "This component's Functional Design, requirements, Test Plan, Tech Stack, the "
            "Functional Designs of consumed/consuming components, and the current stub code "
            "(written by test_coder) to supersede. Never the test source or peer code."
        ),
        require_responsibility=True,
    ),
    output_schema=author_output(),
)

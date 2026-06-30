"""SubAgentSpec for ``test_designer`` (stage 5 author, critic = test_design_critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["TEST_DESIGNER"]


TEST_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="test_designer",
    description="Produces the per-component Test Plan of behavioral test entries.",
    input_schema=pipeline_input(
        input_paths=(
            "Must include this component's Functional Design, the requirements, and the Tech Stack."
        ),
        require_responsibility=True,
    ),
    output_schema=author_output(),
)

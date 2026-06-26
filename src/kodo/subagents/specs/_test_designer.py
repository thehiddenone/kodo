"""SubAgentSpec for ``test_designer`` (stage 5 author, critic = test_coder)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["TEST_DESIGNER"]


TEST_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="test_designer",
    description="Produces the per-component Test Plan of behavioral test entries.",
    input_schema=pipeline_input(
        input_artifacts=(
            "Must include this component's Functional Design (type=functional-design), the "
            "requirements (type=requirements), and the Tech Stack (type=tech-stack)."
        ),
        require_responsibility=True,
    ),
    output_schema=author_output(),
)

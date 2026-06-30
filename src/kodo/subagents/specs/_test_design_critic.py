"""SubAgentSpec for ``test_design_critic`` (stage 5 critic of ``test_designer``)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["TEST_DESIGN_CRITIC"]


TEST_DESIGN_CRITIC: SubAgentSpec = SubAgentSpec(
    name="test_design_critic",
    description=(
        "Reviews the per-component Test Plan, holding every test to behavior over implementation."
    ),
    input_schema=pipeline_input(
        input_paths=(
            "The Test Plan under review, plus this component's Functional Design, the "
            "requirements, and the Tech Stack."
        ),
        require_responsibility=True,
    ),
    output_schema=critic_output(
        [
            "non_behavioral_test",
            "over_specified_test",
            "compound_test",
            "ungrounded_test",
            "coverage_gap",
            "ambiguity",
        ]
    ),
)

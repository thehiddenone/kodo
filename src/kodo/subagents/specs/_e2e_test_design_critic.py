"""SubAgentSpec for ``e2e_test_design_critic`` (stage 8 critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["E2E_TEST_DESIGN_CRITIC"]


E2E_TEST_DESIGN_CRITIC: SubAgentSpec = SubAgentSpec(
    name="e2e_test_design_critic",
    description="Reviews the End-to-End Test Plan for behavioral, in-scope, well-mocked scenarios.",
    input_schema=pipeline_input(
        input_paths=(
            "The End-to-End Test Plan under review, plus the architecture, requirements, "
            "Narrative, Tech Stack, Design Plan, and all Functional Designs."
        ),
    ),
    output_schema=critic_output(
        [
            "non_behavioral_scenario",
            "out_of_scope_test",
            "requirement_uncovered",
            "mock_underspecified",
            "missing_external_dependency",
            "seam_misuse",
            "ungrounded_or_compound_scenario",
            "ambiguity",
        ]
    ),
)

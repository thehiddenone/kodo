"""SubAgentSpec for ``e2e_test_code_critic`` (stage 9 critic).

Reviews ``e2e_test_coder``'s integration suite as code: enforces opaque-box
discipline (boundary-observable assertions, declared seams, only external
dependencies mocked) plus common-sense integration-suite quality rules. Its
concern list **is** its vocabulary; the first five kinds carry the black-box
mandate, the rest are the common-sense rules.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["E2E_TEST_CODE_CRITIC"]


E2E_TEST_CODE_CRITIC: SubAgentSpec = SubAgentSpec(
    name="e2e_test_code_critic",
    description=(
        "Reviews the end-to-end integration suite as code: enforces opaque-box, behavior-and-"
        "side-effect assertions over implementation details, plus common-sense quality rules."
    ),
    input_schema=pipeline_input(
        input_paths=(
            "The integration-suite file(s) under review (harness, mock servers, configuration "
            "injection, scenario tests), plus the accepted End-to-End Test Plan and the Tech Stack."
        ),
    ),
    output_schema=critic_output(
        [
            "white_box_assertion",
            "seam_bypass",
            "over_mocked_system",
            "non_behavioral_assertion",
            "scenario_fidelity",
            "flakiness",
            "cleanup",
            "security",
            "anti_pattern",
            "dead_code",
            "naming",
            "test_documentation",
        ]
    ),
)

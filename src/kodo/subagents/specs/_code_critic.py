"""SubAgentSpec for ``code_critic`` (stage 7 critic; reviews code and test artifacts)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["CODE_CRITIC"]


CODE_CRITIC: SubAgentSpec = SubAgentSpec(
    name="code_critic",
    description="Reviews a single code or test artifact in isolation for quality and safety.",
    input_schema=pipeline_input(
        input_artifacts=(
            "The code (type=code) or test (type=test) artifact under review. Never the design, "
            "requirements, or peer code."
        ),
    ),
    output_schema=critic_output(
        [
            "security",
            "anti_pattern",
            "dead_code",
            "naming",
            "error_handling",
            "resource_leak",
            "concurrency",
            "logging",
            "documentation",
            "test_quality",
            "over_mocking",
            "test_documentation",
            "cleanup",
        ]
    ),
)

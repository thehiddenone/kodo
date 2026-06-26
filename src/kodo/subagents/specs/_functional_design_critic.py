"""SubAgentSpec for ``functional_design_critic`` (stage 4 critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import critic_output, pipeline_input

__all__ = ["FUNCTIONAL_DESIGN_CRITIC"]


FUNCTIONAL_DESIGN_CRITIC: SubAgentSpec = SubAgentSpec(
    name="functional_design_critic",
    description="Reviews a Functional Design for completeness and interface consistency.",
    input_schema=pipeline_input(
        input_artifacts=(
            "The Functional Design under review (type=functional-design), the Design Plan "
            "(type=design-plan), the upstream documents, and any locked peer Functional Designs "
            "for interface-consistency checks."
        ),
    ),
    output_schema=critic_output(
        [
            "not_functional",
            "requirement_uncovered",
            "interface_incompleteness",
            "interface_mismatch",
            "contradiction",
            "missing_failure_mode",
            "ambiguity",
        ]
    ),
)

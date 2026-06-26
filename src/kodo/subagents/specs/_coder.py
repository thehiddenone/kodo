"""SubAgentSpec for ``coder`` (stage 7 author, paired with code_critic)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["CODER"]


CODER: SubAgentSpec = SubAgentSpec(
    name="coder",
    description="Writes the production implementation for a component so its tests pass.",
    input_schema=pipeline_input(
        input_artifacts=(
            "This component's Functional Design (type=functional-design), requirements "
            "(type=requirements), Test Plan (type=test-plan), Tech Stack (type=tech-stack), the "
            "Functional Designs of consumed/consuming components, and the current stub artifacts "
            "(type=code, author=test_coder) to supersede. Never the test source or peer code."
        ),
        require_responsibility=True,
    ),
    output_schema=author_output(
        extra_properties={
            "routed_feedback_artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "IDs of any feedback artifacts this round raised to upstream agents "
                    "(suspected_test_bug to test_coder, spec_ambiguity to functional_designer)."
                ),
            },
        },
    ),
)

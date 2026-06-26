"""SubAgentSpec for ``e2e_test_designer`` (stage 8 author)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["E2E_TEST_DESIGNER"]


E2E_TEST_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="e2e_test_designer",
    description="Produces the End-to-End Test Plan against mocked external dependencies.",
    input_schema=pipeline_input(
        input_artifacts=(
            "The architecture (type=architecture, Part 3 verdict + seams), requirements "
            "(type=requirements), Narrative (type=narrative), Tech Stack (type=tech-stack), the "
            "Design Plan (type=design-plan), and all Functional Designs (type=functional-design)."
        ),
    ),
    output_schema=author_output(
        extra_properties={
            "missing_test_seam": {
                "type": "boolean",
                "description": (
                    "True when an external dependency lacks a declared configuration seam, so a "
                    "missing_test_seam feedback was raised and the plan is blocked pending a fix."
                ),
            },
        },
    ),
)

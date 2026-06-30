"""SubAgentSpec for ``test_coder`` — a dual-role agent.

``test_coder`` is both **test_designer's critic** (stage 5: it validates a Test
Plan for behavioral soundness, returning a verdict + ``non_behavioral_test``
concerns) **and** a **solo author** (stage 6: it writes the test code and minimal
production stubs). A single spec therefore declares a top-level ``oneOf``
``output_schema``: ``return_result`` accepts whichever branch matches the current
invocation, and the engine normalizes the payload against the matching branch.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, critic_output, pipeline_input

__all__ = ["TEST_CODER"]


TEST_CODER: SubAgentSpec = SubAgentSpec(
    name="test_coder",
    description=(
        "As critic, validates a Test Plan for behavioral soundness; as solo author, writes the "
        "test code and minimal production stubs (all tests failing — the TDD starting state)."
    ),
    input_schema=pipeline_input(
        input_paths="The Test Plan, the Functional Design, the Tech Stack, and the requirements.",
        require_responsibility=True,
    ),
    output_schema={
        "oneOf": [
            author_output(),  # stage 6: test + stub code written
            critic_output(["non_behavioral_test"]),  # stage 5: Test Plan validation
        ],
    },
)

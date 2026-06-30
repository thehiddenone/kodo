"""SubAgentSpec for ``test_coder`` — a solo author.

``test_coder`` is the **solo author** (stage 6) that writes the test code and
minimal production stubs for a component from a Test Plan that ``test_designer``
wrote and ``test_design_critic`` already accepted (all tests failing — the TDD
starting state). It no longer reviews the plan: behavioral review of the Test
Plan moved to ``test_design_critic``, so this spec is a plain author output, not
a dual-role ``oneOf``.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["TEST_CODER"]


TEST_CODER: SubAgentSpec = SubAgentSpec(
    name="test_coder",
    description=(
        "Solo author: writes the test code and minimal production stubs for a component from the "
        "accepted Test Plan (all tests failing — the TDD starting state)."
    ),
    input_schema=pipeline_input(
        input_paths="The Test Plan, the Functional Design, the Tech Stack, and the requirements.",
        require_responsibility=True,
    ),
    output_schema=author_output(),
)

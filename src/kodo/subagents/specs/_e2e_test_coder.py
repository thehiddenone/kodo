"""SubAgentSpec for ``e2e_test_coder`` (stage 9 author, paired with e2e_test_code_critic).

``e2e_test_coder`` implements the product-level integration suite the accepted
End-to-End Test Plan designs: the harness that assembles the whole system as a
black box, the local mock servers standing in for external dependencies, the
configuration injection through the declared seams, and the behavioral
assertions per scenario. It runs the suite via ``toolchain_build`` and iterates
to a clean state before the critic sees it; a genuine system-behavior mismatch
is surfaced to the guide via ``escalate_blocker`` rather than papered over.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["E2E_TEST_CODER"]


E2E_TEST_CODER: SubAgentSpec = SubAgentSpec(
    name="e2e_test_coder",
    description=(
        "Implements and runs the product-level end-to-end integration suite from the accepted "
        "End-to-End Test Plan: harness assembling the whole system as a black box, local mock "
        "servers for external dependencies, configuration injection, and behavioral assertions."
    ),
    input_schema=pipeline_input(
        input_paths=(
            "The End-to-End Test Plan (inventory + Mock Specifications + scenarios), the "
            "architecture (Part 3 seams), the Tech Stack, the requirements, the Narrative, the "
            "Design Plan, and all Functional Designs (consumed external interfaces + seams). "
            "Production code may be read only to learn the system's boundary (entry point, "
            "config), never to assert on internals."
        ),
    ),
    output_schema=author_output(),
)

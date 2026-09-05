"""SubAgentSpec for ``e2e_test_designer`` (stage 8 author)."""

from __future__ import annotations

from .._artifacts import (
    PRODUCES_REMAINDER,
    ROLE_ARCHITECTURE,
    ROLE_DESIGN_PLAN,
    ROLE_E2E_TEST_PLAN,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_NARRATIVE,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    SCOPE_ALL,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["E2E_TEST_DESIGNER"]


E2E_TEST_DESIGNER: SubAgentSpec = SubAgentSpec(
    name="e2e_test_designer",
    input_schema=pipeline_input(
        input_paths=(
            "The architecture (Part 3 verdict + seams), requirements, Narrative, Tech Stack, "
            "the Design Plan, and all Functional Designs."
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
    produces={ROLE_E2E_TEST_PLAN: PRODUCES_REMAINDER},
    consumes=(
        Need(ROLE_ARCHITECTURE),
        Need(ROLE_REQUIREMENTS),
        Need(ROLE_NARRATIVE),
        Need(ROLE_TECH_STACK),
        Need(ROLE_DESIGN_PLAN),
        Need(ROLE_FUNCTIONAL_DESIGN, SCOPE_ALL),
    ),
)

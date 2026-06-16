"""``toolchain_test`` tool spec — placeholder, dispatch not yet implemented."""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["TOOLCHAIN_TEST"]


TOOLCHAIN_TEST: ToolSpec = ToolSpec(
    name="toolchain_test",
    external_name="Run Tests",
    user_description="Run the test suite",
    description=(
        "Run the component's test suite and return the execution log: pass/fail "
        "status per test, error codes, assertion failures, and stack traces."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    when_to_use=(
        "After a successful build, to check whether tests pass and to "
        "diagnose failures (implementation bug vs. test bug vs. spec "
        "ambiguity).",
        "After each refactor change, to confirm tests remain green.",
        "After addressing review feedback or user feedback that touches "
        "code, to confirm tests still pass (or to detect that feedback "
        "breaks tests, triggering `escalate_blocker` with `reason: "
        '"feedback_breaks_tests"`).',
    ),
)

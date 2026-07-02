"""SubAgentSpec for ``developer`` — a standalone code+tests sub-agent.

The Developer is Coder and Test Coder combined into one on-demand agent: given
free-form ``instructions`` (from the user, an investigation, or a plan step) it
works out the target behavior, writes the production code, and writes
behavior-based tests for it, keeping the project buildable. Unlike the pipeline
Coder/Test Coder it has no upstream Functional Design / Test Plan artifacts and
no critic loop — it is driven directly by the Problem Solver.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["DEVELOPER"]


DEVELOPER: SubAgentSpec = SubAgentSpec(
    name="developer",
    description=(
        "Coder and Test Coder in one: from free-form instructions, writes the production code and "
        "behavior-based tests for it and keeps the project building."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "description": (
                    "What to build or change, in free form. Describe the desired behavior; the "
                    "Developer works out the implementation and the tests. Include any "
                    "constraints, conventions, and acceptance criteria known to the caller."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional supporting context — investigation findings, prior step outputs, "
                    "relevant decisions — that informs the work but isn't itself an instruction."
                ),
            },
            "input_paths": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Optional named collection (label -> absolute path) of existing files the "
                    "Developer should read for context or edit, e.g. "
                    '{"module": "src/app/orders.py"}.'
                ),
            },
            "write_tests": {
                "type": "boolean",
                "description": (
                    "Whether behavioral tests are wanted (default true). The caller sets this "
                    "false when the user opted out of test coverage for this task."
                ),
            },
        },
        "required": ["instructions"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "primary_path": {
                "type": "string",
                "description": "The main file produced or changed this run.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Every path created or edited (production and test).",
            },
            "tests_written": {
                "type": "boolean",
                "description": "Whether behavioral tests were written this run.",
            },
            "verification": {
                "type": "string",
                "description": (
                    "How the work was verified — build/test outcome from toolchain_build. If no "
                    "build scripts exist, start this with the token 'toolchain_not_set_up' so the "
                    "Problem Solver knows to set the toolchain up and re-run the task."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One line: what was built or changed. No file content.",
            },
        },
        "required": ["primary_path", "paths", "summary"],
    },
)

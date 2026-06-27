"""``toolchain_build`` tool spec — placeholder, dispatch not yet implemented.

One tool that drives the project's standard build scripts (the per-platform
``scripts/<step>.{sh,ps1}`` pairs a toolchain-setup agent generates: ``build``,
``static_analysis``, ``test``, ``format``). Boolean flags select which steps to
run; the steps always run in canonical order — **format → build →
static_analysis → test** — and stop at the first failure. It absorbs the former
separate ``toolchain_test`` tool: run only the tests by enabling ``test`` and
disabling the others.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["TOOLCHAIN_BUILD"]


TOOLCHAIN_BUILD: ToolSpec = ToolSpec(
    name="toolchain_build",
    external_name="Build & Test Project",
    user_description="Build, analyze, and test the project",
    description=(
        "Run the project's standard build steps in the language/tooling declared "
        "by the Tech Stack, by invoking the toolchain's generated build scripts. "
        "Boolean flags select which steps run; enabled steps always run in this "
        "order and stop at the first failure: **format → build → "
        "static_analysis → test**.\n\n"
        "Steps (each maps to the matching `scripts/<step>` script):\n"
        "- `build` (default true) — compile/build the project.\n"
        "- `static_analysis` (default true) — lint, style, and type checks.\n"
        "- `test` (default true) — run the test suite.\n"
        "- `format` (default false) — auto-format the source in place; off by "
        "default because it mutates files.\n\n"
        "To run only the tests (the former `toolchain_test`), enable `test` and "
        "disable `build` and `static_analysis`. Use `test_selector` to run a "
        "single test or suite in isolation — it is passed through to the `test` "
        "script's selector argument; omit it to run the whole suite.\n\n"
        "Returns overall success plus, per step that ran, its success and output "
        "log (build errors, lint findings, test failures and stack traces)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "build": {
                "type": "boolean",
                "description": "Run the build step (`scripts/build`). Default true.",
            },
            "static_analysis": {
                "type": "boolean",
                "description": (
                    "Run static analysis (`scripts/static_analysis`) — lint, style, "
                    "and type checks. Default true."
                ),
            },
            "test": {
                "type": "boolean",
                "description": "Run the test suite (`scripts/test`). Default true.",
            },
            "format": {
                "type": "boolean",
                "description": (
                    "Run the formatter (`scripts/format`) in place before building. "
                    "Default false (it mutates source files)."
                ),
            },
            "test_selector": {
                "type": "string",
                "description": (
                    "Optional selector passed to the `test` script to run a single "
                    "test or suite in isolation (mapped to the toolchain's native "
                    "selection syntax). Omit to run the whole suite. Ignored when "
                    "`test` is disabled."
                ),
            },
        },
        "required": [],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {
                "type": "boolean",
                "description": "True only if every step that ran succeeded.",
            },
            "steps": {
                "type": "array",
                "description": (
                    "One entry per step that ran, in execution order, each with the "
                    "step name, its success, and its output log."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {
                            "type": "string",
                            "description": "Step name: format | build | static_analysis | test.",
                        },
                        "success": {
                            "type": "boolean",
                            "description": "Whether this step succeeded.",
                        },
                        "log": {
                            "type": "string",
                            "description": (
                                "The step's output (build errors, lint findings, or "
                                "test pass/fail log with assertions and stack traces)."
                            ),
                        },
                    },
                    "required": ["step", "success", "log"],
                },
            },
        },
        "required": ["success", "steps"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={
        "build": "always",
        "static_analysis": "always",
        "test": "always",
        "format": "always",
        "test_selector": "always",
    },
    output_visibility={"success": "always", "steps": "visible"},
    when_to_use=(
        "After publishing new or superseding `code` artifacts, to confirm the "
        "project builds, passes static analysis, and passes its tests.",
        "After a refactor change, to confirm the build still succeeds and tests "
        "remain green (run with the default steps).",
        "To run just the tests — enable `test`, disable `build` and "
        "`static_analysis` — e.g. to diagnose failures (implementation bug vs. "
        "test bug vs. spec ambiguity), or after addressing review/user feedback "
        "that touches code (detecting that feedback breaks tests triggers "
        '`escalate_blocker` with `reason: "feedback_breaks_tests"`).',
        "To run a single test or suite in isolation, pass `test_selector`.",
    ),
)

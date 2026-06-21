"""``run_command`` tool spec — native shell tool.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves ``working_dir`` against the project root and rejects anything
that would escape it, then runs the command via
:func:`asyncio.create_subprocess_shell`. Neither this spec nor the dispatcher
impose any other restriction on the command itself — that is the security
layer's job.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["RUN_COMMAND"]


RUN_COMMAND: ToolSpec = ToolSpec(
    name="run_command",
    external_name="Run Command",
    user_description="Run shell command",
    description=(
        "Execute a shell command and return its exit code, stdout, and "
        "stderr. Runs via the system shell with stdin closed (commands that "
        "read interactive input get immediate EOF — they never block). "
        "working_dir defaults to the project root and, if given, must resolve "
        "inside it. You MUST supply a timeout (seconds): the command is killed "
        "if it has not finished by then, and the result reports a null exit "
        "code with a 'timed out' note on stderr. Choose a value that comfortably "
        "covers the expected runtime — e.g. ~10s for a quick CLI check, ~120s "
        "for a build, more for a long test suite — but small enough that a hung "
        "command fails fast instead of stalling the agent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Maximum seconds to wait for the command to finish before "
                    "it is killed. Required. Pick a value that fits the expected "
                    "runtime with headroom (e.g. 10 for a quick check, 120 for a "
                    "build); a hung command then fails fast rather than stalling "
                    "the agent."
                ),
                "exclusiveMinimum": 0,
            },
            "working_dir": {
                "type": "string",
                "description": (
                    "Directory to run the command in, relative to the project "
                    "root (or an absolute path inside it). Defaults to the "
                    "project root."
                ),
            },
        },
        "required": ["command", "timeout"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "exit_code": {
                "type": ["integer", "null"],
                "description": "Process exit code (null if it could not be determined).",
            },
            "stdout": {"type": "string", "description": "Captured standard output."},
            "stderr": {"type": "string", "description": "Captured standard error."},
        },
        "required": ["exit_code", "stdout", "stderr"],
    },
    security_impact=SecurityImpact.CRITICAL,
    input_visibility={"command": "always", "timeout": "always", "working_dir": "visible"},
    output_visibility={"exit_code": "always", "stdout": "visible", "stderr": "visible"},
    when_to_use=(
        "Running a command the toolchain tools "
        "(`toolchain_build`/`toolchain_test`/`toolchain_deps`) don't cover "
        "— e.g., a one-off CLI invocation needed to scaffold or inspect the "
        "project.",
    ),
)

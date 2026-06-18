"""``run_command`` tool spec — native shell tool.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves ``working_dir`` against the project root and rejects anything
that would escape it, then runs the command via
:func:`asyncio.create_subprocess_shell`. Neither this spec nor the dispatcher
impose any other restriction on the command itself — that is the security
layer's job.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["RUN_COMMAND"]


RUN_COMMAND: ToolSpec = ToolSpec(
    name="run_command",
    external_name="Run Command",
    user_description="Run a shell command",
    description=(
        "Execute a shell command and return its exit code, stdout, and "
        "stderr. Runs via the system shell. working_dir defaults to the "
        "project root and, if given, must resolve inside it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
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
        "required": ["command"],
    },
    when_to_use=(
        "Running a command the toolchain tools "
        "(`toolchain_build`/`toolchain_test`/`toolchain_deps`) don't cover "
        "— e.g., a one-off CLI invocation needed to scaffold or inspect the "
        "project.",
    ),
)

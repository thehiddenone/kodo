"""``delete_file`` tool spec — native file I/O tool.

Dispatch lives in :class:`~kodo.runtime._subagent_dispatch.SubagentDispatcher`,
which resolves the path against the project root and rejects anything that
would escape it.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["DELETE_FILE"]


DELETE_FILE: ToolSpec = ToolSpec(
    name="delete_file",
    external_name="Delete File",
    user_description="Delete a file",
    description=(
        "Delete a file permanently. Fails if the file does not exist. The "
        "path must resolve inside the project root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file, relative to the project root (or an absolute path "
                    "inside it). Paths that resolve outside the project root are rejected."
                ),
            },
        },
        "required": ["path"],
    },
    when_to_use=(
        "Removing a stale non-artifact file (e.g., a leftover generated "
        "file) that is no longer needed.",
    ),
)

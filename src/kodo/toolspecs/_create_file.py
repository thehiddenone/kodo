"""``create_file`` tool spec — native file I/O tool.

Dispatch lives in :class:`~kodo.runtime._subagent_dispatch.SubagentDispatcher`,
which resolves the path against the project root and rejects anything that
would escape it.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["CREATE_FILE"]


CREATE_FILE: ToolSpec = ToolSpec(
    name="create_file",
    external_name="Create File",
    user_description="Create a new file",
    description=(
        "Create a new file with the given content. Fails if the file already "
        "exists — use edit_file to overwrite an existing file. Parent "
        "directories are created automatically. The path must resolve inside "
        "the project root."
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
            "content": {
                "type": "string",
                "description": "Full content to write to the new file.",
            },
        },
        "required": ["path", "content"],
    },
    when_to_use=(
        "Writing a file directly to the project tree (outside the "
        "`publish_artifact` pipeline) — e.g., scaffolding config files, "
        "lockfiles, or other non-artifact project files a toolchain expects "
        "on disk.",
    ),
)

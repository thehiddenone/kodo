"""``edit_file`` tool spec — native file I/O tool.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves the path against the project root and rejects anything that
would escape it.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["EDIT_FILE"]


EDIT_FILE: ToolSpec = ToolSpec(
    name="edit_file",
    external_name="Edit File",
    user_description="Replace a file's contents",
    description=(
        "Replace the entire content of an existing file. Fails if the file "
        "does not exist — use create_file to create a new file. The path must "
        "resolve inside the project root."
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
                "description": "Full replacement content for the file.",
            },
        },
        "required": ["path", "content"],
    },
    when_to_use=(
        "Updating a non-artifact file already on disk (e.g., a generated "
        "config or lockfile) in place.",
    ),
)

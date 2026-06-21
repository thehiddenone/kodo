"""``create_file`` tool spec — native file I/O tool.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves the path against the project root and rejects anything that
would escape it.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

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
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'created' on success."},
            "path": {"type": "string", "description": "The path that was written."},
        },
        "required": ["status", "path"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"path": "always", "content": "visible"},
    output_visibility={"status": "always", "path": "always"},
    when_to_use=(
        "Writing a file directly to the project tree (outside the "
        "`publish_artifact` pipeline) — e.g., scaffolding config files, "
        "lockfiles, or other non-artifact project files a toolchain expects "
        "on disk.",
    ),
)

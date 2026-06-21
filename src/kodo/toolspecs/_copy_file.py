"""``copy_file`` tool spec — native file I/O tool.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves both paths against the project root and rejects anything that
would escape it.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["COPY_FILE"]


_PATH_PROPERTY = {
    "type": "string",
    "description": (
        "Path to the file, relative to the project root (or an absolute path "
        "inside it). Paths that resolve outside the project root are rejected."
    ),
}


COPY_FILE: ToolSpec = ToolSpec(
    name="copy_file",
    external_name="Copy File",
    user_description="Copy a file",
    description=(
        "Copy a file to a new location under the project root, preserving "
        "metadata. Fails if the source does not exist. Both paths must resolve "
        "inside the project root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source": _PATH_PROPERTY,
            "destination": _PATH_PROPERTY,
        },
        "required": ["source", "destination"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'copied' on success."},
            "source": {"type": "string", "description": "The source path."},
            "destination": {"type": "string", "description": "The destination path."},
        },
        "required": ["status", "source", "destination"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={"source": "always", "destination": "always"},
    output_visibility={"status": "always", "source": "always", "destination": "always"},
    when_to_use=(
        "Duplicating a non-artifact file (e.g., a template) to a new location as a starting point.",
    ),
)

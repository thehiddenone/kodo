"""``copy_file`` tool spec — native file I/O tool.

Dispatch lives in :class:`~kodo.runtime._subagent_dispatch.SubagentDispatcher`,
which resolves both paths against the project root and rejects anything that
would escape it.
"""

from __future__ import annotations

from ._spec import ToolSpec

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
    when_to_use=(
        "Duplicating a non-artifact file (e.g., a template) to a new location as a starting point.",
    ),
)

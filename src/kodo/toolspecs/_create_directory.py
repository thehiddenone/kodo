"""``create_directory`` tool spec — native directory-creation tool.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool), which
resolves the path against the project root and rejects anything that would
escape it.

Split out of the former ``filesystem`` tool's ``create_dir`` operation so
directory creation — a LOW-impact, purely additive action — no longer shares
``filesystem``'s HIGH-impact security posture with delete/copy/move. To
remove, copy, or move whole files or directories, use the ``filesystem`` tool
instead; to create a brand-new file, use ``create_file``.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["CREATE_DIRECTORY"]


CREATE_DIRECTORY: ToolSpec = ToolSpec(
    name="create_directory",
    external_name="Create Directory",
    user_description="Create a directory",
    description=(
        "Creates a directory, including any missing parents (like `mkdir -p`). "
        "Succeeds if it already exists.\n\n"
        "To delete, copy, or move whole files or directories, use the "
        "`filesystem` tool instead; to create a brand-new file, use "
        "`create_file`.\n"
        "The path must resolve inside the project root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "path": {
                "type": "string",
                "description": (
                    "Path to the directory, relative to the project root (or an absolute "
                    "path inside it). Paths that resolve outside the project root are "
                    "rejected."
                ),
            },
        },
        "required": ["intent", "path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'created' on success."},
            "path": {"type": "string", "description": "The path that was created."},
            "checkpoint_sha": {
                "type": "string",
                "description": (
                    "Mirror checkpoint commit recording this creation (present when "
                    "checkpointing is active; absent otherwise)."
                ),
            },
            "checkpoint_root": {
                "type": "string",
                "description": (
                    "Root of the .kodo/checkpoints mirror checkpoint_sha belongs to "
                    "(present alongside checkpoint_sha)."
                ),
            },
        },
        "required": ["status", "path"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={"intent": "always", "path": "always"},
    output_visibility={"status": "always", "path": "always"},
    when_to_use=(
        "Creating a directory, including any missing parents — e.g. scaffolding "
        "a non-artifact project directory a toolchain expects on disk. Succeeds "
        "even if the directory already exists. To delete, copy, or move whole "
        "files or directories, use the `filesystem` tool; to create a "
        "brand-new file, use `create_file`.",
    ),
)

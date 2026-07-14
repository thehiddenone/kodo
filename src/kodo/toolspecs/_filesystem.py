"""``filesystem`` tool spec — one native tool for every file/directory operation.

A single tool that performs six filesystem operations — selected by the
mandatory ``operation`` field — instead of a separate tool per operation. It
replaces the former ``delete_file`` / ``copy_file`` / ``move_file`` tools and
adds the directory counterparts. It deliberately does **not** subsume
``create_file`` (creating a brand-new file), ``create_directory`` (creating a
directory), or ``edit_file`` (the targeted, preferred way to change an
existing file's contents), which are their own tools.

Dispatch lives in :mod:`kodo.tools` (``_filesystem.py``), which resolves every
path against the project root and rejects anything that would escape it.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["FILESYSTEM"]


# The six operations, grouped by the arguments they consume:
#   path           : delete_file, delete_dir
#   source + dest  : copy_file, move_file, copy_dir, move_dir
_OPERATIONS = (
    "delete_file",
    "delete_dir",
    "copy_file",
    "copy_dir",
    "move_file",
    "move_dir",
)

_PATH_DESC = (
    "Path relative to the project root (or an absolute path inside it). Paths "
    "that resolve outside the project root are rejected — unless `temporary` "
    "is true, in which case every path resolves under the session's scratch "
    "directory instead."
)


FILESYSTEM: ToolSpec = ToolSpec(
    name="filesystem",
    external_name="Filesystem",
    user_description="Delete, copy, or move files and directories",
    description=(
        "The single tool for filesystem operations on files AND directories: "
        "deleting, copying, moving, and renaming them. Pick the operation with "
        "the required `operation` field; the other arguments you must supply "
        "depend on it. Every path must resolve inside the project root.\n\n"
        "Operations and their arguments:\n"
        "- `delete_file` — needs `path`. Permanently deletes a file. Fails if it "
        "does not exist or is a directory.\n"
        "- `delete_dir` — needs `path`. Permanently deletes a directory AND all "
        "of its contents, recursively. Fails if it does not exist or is a file.\n"
        "- `copy_file` — needs `source` + `destination`. Copies a file, "
        "preserving metadata. Fails if `source` does not exist.\n"
        "- `copy_dir` — needs `source` + `destination`. Recursively copies a "
        "directory tree. Fails if `source` does not exist or `destination` "
        "already exists.\n"
        "- `move_file` — needs `source` + `destination`. Moves or renames a file. "
        "Fails if `source` does not exist.\n"
        "- `move_dir` — needs `source` + `destination`. Moves or renames a "
        "directory tree. Fails if `source` does not exist.\n\n"
        "To create a directory, use `create_directory` instead; to create a "
        "brand-new file, use `create_file` instead; to change the contents of "
        "an existing file, use `edit_file` instead — this tool only "
        "removes/relocates whole files and directories.\n\n"
        "Set `temporary: true` to operate entirely within the session's "
        "private scratch directory instead of the project (see `temporary` "
        "below)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "operation": {
                "type": "string",
                "enum": list(_OPERATIONS),
                "description": (
                    "Which filesystem operation to perform. Determines which of "
                    "the other arguments are required: `path` for the delete "
                    "operations, or `source`+`destination` for the copy/move "
                    "operations."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Target path for the delete operations (`delete_file`, "
                    "`delete_dir`). " + _PATH_DESC
                ),
            },
            "source": {
                "type": "string",
                "description": (
                    "Source path for the copy/move operations (`copy_file`, "
                    "`copy_dir`, `move_file`, `move_dir`). " + _PATH_DESC
                ),
            },
            "destination": {
                "type": "string",
                "description": ("Destination path for the copy/move operations. " + _PATH_DESC),
            },
            "temporary": {
                "type": "boolean",
                "description": (
                    "When true, every path this call touches (`path`, `source`, "
                    "`destination`) resolves under this session's private scratch "
                    "directory instead of the project root — relative paths land "
                    "inside it, absolute paths must already be inside it. Use this for "
                    "throwaway work you don't want in the project itself. Changes made "
                    "there are never captured by the project's checkpoint/rollback "
                    "mirror, and this call is always allowed without a permission "
                    "prompt (including `delete_dir`), regardless of Command Control "
                    "posture. Default false."
                ),
            },
        },
        "required": ["intent", "operation"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "The completed operation's past tense on success — "
                    "'deleted', 'copied', or 'moved'."
                ),
            },
            "operation": {
                "type": "string",
                "description": "The operation that was requested.",
            },
            "path": {
                "type": "string",
                "description": "The path acted on (delete operations).",
            },
            "source": {
                "type": "string",
                "description": "The source path (copy/move operations).",
            },
            "destination": {
                "type": "string",
                "description": "The destination path (copy/move operations).",
            },
            "checkpoint_sha": {
                "type": "string",
                "description": (
                    "Mirror checkpoint commit recording this operation (present when "
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
        "required": ["status", "operation"],
    },
    security_impact=SecurityImpact.HIGH,
    input_visibility={
        "intent": "always",
        "operation": "always",
        "path": "always",
        "source": "always",
        "destination": "always",
        "temporary": "visible",
    },
    output_visibility={
        "status": "always",
        "operation": "always",
        "path": "always",
        "source": "always",
        "destination": "always",
    },
    when_to_use=(
        "Any time you need to delete, copy, move, or rename a file or a "
        "directory on disk — this one tool covers all of those. Set "
        "`operation` to the action you want (e.g. `delete_dir`, `copy_dir`, "
        "`move_file`).",
        "Removing stale files or whole directories, or relocating/renaming them.",
        "Use `create_directory` instead to create a directory, `create_file` "
        "to create a brand-new file, or `edit_file` to change the CONTENTS of "
        "an existing file — this tool does not create directories or edit "
        "file contents.",
        "Pass `temporary: true` to operate in the session's private scratch "
        "directory instead of the project — for throwaway work you don't want "
        "checkpointed, reviewed, or left in the project tree; this also lifts "
        "the `delete_dir` permission prompt, since nothing there is tracked.",
    ),
)

"""``filesystem`` tool spec — one native tool for every file/directory operation.

A single tool that performs eight filesystem operations — selected by the
mandatory ``operation`` field — instead of a separate tool per operation. It
replaces the former ``create_file`` / ``delete_file`` / ``copy_file`` /
``move_file`` tools and adds the directory counterparts. It deliberately does
**not** subsume ``edit_file`` (the targeted, preferred way to change an existing
file's contents), which remains its own tool.

Dispatch lives in :mod:`kodo.tools` (``_filesystem.py``), which resolves every
path against the project root and rejects anything that would escape it.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["FILESYSTEM"]


# The eight operations, grouped by the arguments they consume:
#   path + content : create_file
#   path           : create_dir, delete_file, delete_dir
#   source + dest  : copy_file, move_file, copy_dir, move_dir
_OPERATIONS = (
    "create_file",
    "create_dir",
    "delete_file",
    "delete_dir",
    "copy_file",
    "copy_dir",
    "move_file",
    "move_dir",
)

_PATH_DESC = (
    "Path relative to the project root (or an absolute path inside it). Paths "
    "that resolve outside the project root are rejected."
)


FILESYSTEM: ToolSpec = ToolSpec(
    name="filesystem",
    external_name="Filesystem",
    user_description="Create, delete, copy, or move files and directories",
    description=(
        "The single tool for filesystem operations on files AND directories: "
        "creating, deleting, copying, moving, and renaming them. Pick the "
        "operation with the required `operation` field; the other arguments you "
        "must supply depend on it. Every path must resolve inside the project "
        "root.\n\n"
        "Operations and their arguments:\n"
        "- `create_file` — needs `path` + `content`. Creates a new file with "
        "that content. Fails if it already exists (use `edit_file` to change an "
        "existing file's contents). Parent directories are created automatically.\n"
        "- `create_dir` — needs `path`. Creates a directory, including any "
        "missing parents (like `mkdir -p`). Succeeds if it already exists.\n"
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
        "To change the contents of an existing file, use `edit_file` instead — "
        "this tool only creates, removes, or relocates whole files and "
        "directories."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": list(_OPERATIONS),
                "description": (
                    "Which filesystem operation to perform. Determines which of "
                    "the other arguments are required: `path` (+`content` for "
                    "`create_file`) for the create/delete operations, or "
                    "`source`+`destination` for the copy/move operations."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Target path for the create/delete operations (`create_file`, "
                    "`create_dir`, `delete_file`, `delete_dir`). " + _PATH_DESC
                ),
            },
            "content": {
                "type": "string",
                "description": "Full content to write. Required for `create_file` only.",
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
        },
        "required": ["operation"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "The completed operation's past tense on success — 'created', "
                    "'deleted', 'copied', or 'moved'."
                ),
            },
            "operation": {
                "type": "string",
                "description": "The operation that was requested.",
            },
            "path": {
                "type": "string",
                "description": "The path acted on (create/delete operations).",
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
        "operation": "always",
        "path": "always",
        "content": "visible",
        "source": "always",
        "destination": "always",
    },
    output_visibility={
        "status": "always",
        "operation": "always",
        "path": "always",
        "source": "always",
        "destination": "always",
    },
    when_to_use=(
        "Any time you need to create, delete, copy, move, or rename a file or a "
        "directory on disk — this one tool covers all of those. Set `operation` "
        "to the file or directory action you want (e.g. `create_file`, "
        "`create_dir`, `delete_dir`, `move_file`).",
        "Scaffolding non-artifact project files or directories a toolchain "
        "expects on disk (config files, lockfiles, source/test folders).",
        "Removing stale files or whole directories, or relocating/renaming them.",
        "Use `edit_file` instead when you only need to change the CONTENTS of an "
        "existing file — this tool does not edit file contents.",
    ),
)

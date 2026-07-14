"""``create_file`` tool spec — native file I/O tool (whole-file creation).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves the path against the project root and rejects anything that
would escape it.

This is the **preferred** way to create a brand-new file: it writes `content`
verbatim at `path` and never touches an existing file. To change an existing
file's contents, use ``edit_file`` instead; to delete, copy, or move whole
files or directories, use the ``filesystem`` tool.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["CREATE_FILE"]


CREATE_FILE: ToolSpec = ToolSpec(
    name="create_file",
    external_name="Create File",
    user_description="Create a brand-new file",
    description=(
        "The PREFERRED way to create a brand-new file. Writes `content` "
        "verbatim at `path`, creating any missing parent directories.\n\n"
        "Rules:\n"
        "- Fails if a file already exists at `path` — nothing is written, and "
        "the existing file is left untouched. Use `edit_file` instead to "
        "change an existing file's contents.\n"
        "- `content` is the file's entire contents; there is no partial-file "
        "mode.\n"
        "To delete, copy, or move whole files or directories, use the "
        "`filesystem` tool instead.\n"
        "The path must resolve inside the project root, unless `temporary` "
        "is true (see below)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "path": {
                "type": "string",
                "description": (
                    "Path to the file, relative to the project root (or an absolute path "
                    "inside it). Paths that resolve outside the project root are rejected "
                    "— unless `temporary` is true, in which case this resolves under the "
                    "session's scratch directory instead."
                ),
            },
            "content": {
                "type": "string",
                "description": "The full content to write to the new file.",
            },
            "temporary": {
                "type": "boolean",
                "description": (
                    "When true, `path` resolves under this session's private scratch "
                    "directory instead of the project root — relative paths land inside "
                    "it, absolute paths must already be inside it. Use this for "
                    "throwaway work you don't want in the project itself: scratch notes, "
                    "intermediate working files, drafts you'll inspect and discard. "
                    "Changes made there are never captured by the project's "
                    "checkpoint/rollback mirror, and this call is always allowed without "
                    "a permission prompt, regardless of Command Control posture. Default "
                    "false."
                ),
            },
        },
        "required": ["intent", "path", "content"],
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
    input_visibility={
        "intent": "always",
        "path": "always",
        "content": "visible",
        "temporary": "visible",
    },
    output_visibility={"status": "always", "path": "always"},
    when_to_use=(
        "Creating a brand-new file — the default, preferred way to add one. "
        "Fails loudly instead of overwriting if the file already exists, so "
        "the model cannot silently clobber existing content. To change an "
        "existing file's contents, use `edit_file`; to delete, copy, or move "
        "whole files or directories, use the `filesystem` tool.",
        "Pass `temporary: true` to write into the session's private scratch "
        "directory instead of the project — for throwaway files you don't "
        "want checkpointed, reviewed, or left in the project tree.",
    ),
)

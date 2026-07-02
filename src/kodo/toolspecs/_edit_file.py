"""``edit_file`` tool spec — native file I/O tool (targeted string-match edit).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves the path against the project root and rejects anything that
would escape it.

This is the **preferred** way to change an existing file: it replaces one exact,
unique snippet (``old_string``) with ``new_string`` and leaves the rest of the
file byte-for-byte untouched. To create, delete, copy, or move whole files or
directories, use the ``filesystem`` tool instead.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["EDIT_FILE"]


EDIT_FILE: ToolSpec = ToolSpec(
    name="edit_file",
    external_name="Edit File",
    user_description="Make a targeted edit to a file",
    description=(
        "The PREFERRED way to edit an existing file. Replaces one exact "
        "occurrence of `old_string` with `new_string`, leaving the rest of the "
        "file untouched. Use this for ordinary, localized changes instead of "
        "rewriting the whole file.\n\n"
        "Rules:\n"
        "- `old_string` must match the file content EXACTLY, including "
        "whitespace and indentation, and must appear EXACTLY ONCE. If it is not "
        "found, or appears more than once, the edit fails and nothing is "
        "written — include enough surrounding context to make the match unique.\n"
        "- To make several changes to one file, call this tool once per change. "
        "Read the file first so your `old_string` reflects the current content; "
        "after an edit, earlier line positions and surrounding text may have "
        "shifted.\n"
        "- To regenerate an entire file end to end, pass its whole new content "
        "as `new_string` and its whole current content as `old_string`.\n"
        "- Fails if the file does not exist — use the `filesystem` tool "
        '(`operation: "create_file"`) to create one.\n'
        "The path must resolve inside the project root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "path": {
                "type": "string",
                "description": (
                    "Path to the file, relative to the project root (or an absolute path "
                    "inside it). Paths that resolve outside the project root are rejected."
                ),
            },
            "old_string": {
                "type": "string",
                "description": (
                    "The exact text to replace. Must match the file content "
                    "verbatim (including whitespace/indentation) and occur exactly "
                    "once. Include surrounding context if needed to make it unique."
                ),
            },
            "new_string": {
                "type": "string",
                "description": (
                    "The text to substitute in place of `old_string`. Use an empty "
                    "string to delete the matched text."
                ),
            },
        },
        "required": ["intent", "path", "old_string", "new_string"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'edited' on success."},
            "path": {"type": "string", "description": "The path that was edited."},
            "checkpoint_sha": {
                "type": "string",
                "description": (
                    "Mirror checkpoint commit recording this edit (present when "
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
    security_impact=SecurityImpact.MODERATE,
    input_visibility={
        "intent": "always",
        "path": "always",
        "old_string": "visible",
        "new_string": "visible",
    },
    output_visibility={"status": "always", "path": "always"},
    when_to_use=(
        "Making a localized change to an existing file — the default, preferred "
        "way to edit. Replaces just the snippet you target and preserves "
        "everything else, keeping the diff minimal. To create, delete, copy, or "
        "move whole files or directories, use the `filesystem` tool.",
    ),
)

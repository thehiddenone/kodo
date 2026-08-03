"""``edit_file`` tool spec — native file I/O tool (targeted string-match edit).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool), which
resolves the path via ``LogicalPathResolver``: a relative path's first
segment must name a bound root (see ``get_root_paths``); an absolute path is
taken as-is, unrestricted.

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
        "`path` is a logical path (folder-prefixed with a bound root's name, "
        "e.g. `billing-service/specs/foo.md`) or an absolute path, unless "
        "`temporary` is true (see below).\n\n"
        "When to use: changing an existing file — this is the default way to do "
        "it, and it keeps the diff minimal. To create, delete, copy, or move "
        "whole files or directories, use the `filesystem` tool. Pass "
        "`temporary: true` to edit a file in the session's private scratch "
        "directory instead of the project, for throwaway work you don't want "
        "checkpointed, reviewed, or left in the project tree.\n\n"
        "The user may reject this call (Edit Control review). A `rejected` "
        "result means try a different approach or ask the user what they "
        "want instead. A `rejected_with_feedback` result includes a "
        "`feedback` array — each entry has the user's `feedback` text and a "
        "`general_feedback` flag: when false, the entry also names a "
        "`targeted_code` snippet (with `line_from`/`line_to`) it targets; "
        "when true, it's a general note about the file as a whole with no "
        "particular line. Address every entry and retry this same call with "
        "revised `old_string`/`new_string` that incorporates the feedback."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "path": {
                "type": "string",
                "description": (
                    "Path to the file: a logical path whose first segment is a bound "
                    "root's name (see `get_root_paths`) — the rest resolves under that "
                    "root — or an absolute path, used as-is. Unless `temporary` is true, "
                    "in which case this must be a relative path that resolves under the "
                    "session's scratch directory instead — an absolute path is going to "
                    "fail, since that directory isn't a path you're given."
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
            "temporary": {
                "type": "boolean",
                "description": (
                    "When true, `path` resolves under this session's private scratch "
                    "directory instead of the project root. `path` must be relative — "
                    "you don't know where that directory lives on disk, so an absolute "
                    "`path` is going to fail. Use this for throwaway work you don't want "
                    "in the project itself. Changes made there are never captured by "
                    "the project's checkpoint/rollback mirror, and this call is always "
                    "allowed without a permission prompt, regardless of Command Control "
                    "posture. Default false."
                ),
            },
        },
        "required": ["intent", "path", "old_string", "new_string"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["edited", "rejected", "rejected_with_feedback"],
                "description": (
                    "'edited' on success. 'rejected' when the user declined the Edit "
                    "Control review gate — nothing was written. 'rejected_with_feedback' "
                    "when they declined with inline feedback (see `feedback`)."
                ),
            },
            "path": {"type": "string", "description": "The path that was (or would be) edited."},
            "feedback": {
                "type": "array",
                "description": (
                    "Present only when `status` is 'rejected_with_feedback'. One entry "
                    "per note the user attached to a selection in the proposed content, "
                    "in the order they were added."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "general_feedback": {
                            "type": "boolean",
                            "description": (
                                "True when this note isn't anchored to any particular line "
                                "(the user added it with nothing selected) — `line_from`/"
                                "`line_to`/`targeted_code` are absent. False for a "
                                "line-anchored note."
                            ),
                        },
                        "line_from": {
                            "type": "integer",
                            "description": (
                                "1-based start line in the proposed content. Absent when "
                                "`general_feedback` is true."
                            ),
                        },
                        "line_to": {
                            "type": "integer",
                            "description": (
                                "1-based end line in the proposed content. Absent when "
                                "`general_feedback` is true."
                            ),
                        },
                        "targeted_code": {
                            "type": "string",
                            "description": (
                                "The exact selected text the note targets. Absent when "
                                "`general_feedback` is true."
                            ),
                        },
                        "feedback": {
                            "type": "string",
                            "description": "The user's free-text note.",
                        },
                    },
                    "required": ["general_feedback", "feedback"],
                },
            },
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
        "temporary": "visible",
    },
    output_visibility={"status": "always", "path": "always", "feedback": "visible"},
    requires_project=False,
    modifies_files=True,
)

"""``create_file`` tool spec — native file I/O tool (whole-file creation).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool), which
resolves the path via ``LogicalPathResolver``: a relative path's first
segment must name a bound root (see ``get_root_paths``); an absolute path is
taken as-is, unrestricted.

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
        "`path` is a logical path (folder-prefixed with a bound root's name, "
        "e.g. `billing-service/specs/foo.md`) or an absolute path, unless "
        "`temporary` is true (see below).\n\n"
        "When to use: adding a file that does not exist yet — this is the "
        "default way to do it. Pass `temporary: true` to write into the "
        "session's private scratch directory instead of the project, for "
        "throwaway files you don't want checkpointed, reviewed, or left in the "
        "project tree.\n\n"
        "The user may reject this call (Edit Control review). A `rejected` "
        "result means try a different approach or ask the user what they "
        "want instead. A `rejected_with_feedback` result includes a "
        "`feedback` array — each entry has the user's `feedback` text and a "
        "`general_feedback` flag: when false, the entry also names a "
        "`targeted_code` snippet (with `line_from`/`line_to`) it targets; "
        "when true, it's a general note about the file as a whole with no "
        "particular line. Address every entry and retry this same call with "
        "revised `content` that incorporates the feedback."
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
                    "fail, since that directory isn't a path you're given. To refer to "
                    "the file again later, reuse the `path` from this call's output."
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
                    "directory instead of the project root. `path` must be relative — "
                    "you don't know where that directory lives on disk, so an absolute "
                    "`path` is going to fail. Use this for throwaway work you don't want "
                    "in the project itself: scratch notes, intermediate working files, "
                    "drafts you'll inspect and discard. Changes made there are never "
                    "captured by the project's checkpoint/rollback mirror, and this call "
                    "is always allowed without a permission prompt, regardless of "
                    "Command Control posture. Default false."
                ),
            },
        },
        "required": ["intent", "path", "content"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["created", "rejected", "rejected_with_feedback"],
                "description": (
                    "'created' on success. 'rejected' when the user declined the Edit "
                    "Control review gate — nothing was written. 'rejected_with_feedback' "
                    "when they declined with inline feedback (see `feedback`)."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "The path that was (or would be) created. Echoed back as given, "
                    "except under `temporary: true`, where the resolved absolute "
                    "filesystem path is returned instead, since the scratch directory's "
                    "location isn't otherwise known. Reuse this value to refer to the "
                    "file again later."
                ),
            },
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
    output_visibility={"status": "always", "path": "always", "feedback": "visible"},
    requires_project=False,
    modifies_files=True,
)

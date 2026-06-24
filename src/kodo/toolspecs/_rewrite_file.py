"""``rewrite_file`` tool spec — native file I/O tool (whole-file replacement).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool),
which resolves the path against the project root and rejects anything that
would escape it.

This is the whole-file counterpart to the targeted ``edit_file`` tool: it
replaces the *entire* content of a file. Agents should prefer ``edit_file`` for
ordinary changes and reach for ``rewrite_file`` only when regenerating a file
end to end.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["REWRITE_FILE"]


REWRITE_FILE: ToolSpec = ToolSpec(
    name="rewrite_file",
    external_name="Rewrite File",
    user_description="Replace a whole file's contents",
    description=(
        "Replace the ENTIRE content of an existing file with the content you "
        "provide. This overwrites everything in the file. You *can* use it to "
        "edit a file, but for most edits you should prefer `edit_file`, which "
        "replaces just the specific text you target and leaves the rest of the "
        "file untouched — it is safer, makes a smaller diff, and avoids "
        "accidentally dropping unrelated content. Reach for `rewrite_file` only "
        "when you are genuinely regenerating the whole file, or when the change "
        "would rewrite most of it anyway. Fails if the file does not exist — use "
        "create_file to create a new file. The path must resolve inside the "
        "project root."
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
                "description": "Full replacement content for the entire file.",
            },
        },
        "required": ["path", "content"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'rewritten' on success."},
            "path": {"type": "string", "description": "The path that was written."},
        },
        "required": ["status", "path"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"path": "always", "content": "visible"},
    output_visibility={"status": "always", "path": "always"},
    when_to_use=(
        "Regenerating a file end to end, or making a change that would rewrite "
        "most of its content anyway (e.g. re-emitting a generated config or "
        "lockfile). For ordinary, localized edits, prefer `edit_file`.",
    ),
)

"""``edit_agent`` tool spec — one exact-string edit to one user agent file.

doc/AGENT_BUILDER.md §6.2: ``edit_file``'s string-match edit, scoped to the
files of one user agent and followed by the same report ``write_agent`` returns.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["EDIT_AGENT"]

_STR = {"type": "string"}

EDIT_AGENT: ToolSpec = ToolSpec(
    name="edit_agent",
    external_name="Edit Agent",
    user_description="Edit a user agent",
    description=(
        "Replace one exact string in one file of a user agent, loaded or broken. "
        "`file` is `prompt`, `config` (a top-level agent's `<name>.json`) or `spec` "
        "(a sub-agent's `<name>.json`). `old_string` must occur exactly once in the "
        "file — copy it from `read_agent`, whitespace included; when it occurs more "
        "than once, add surrounding text until it is unique. The edited file must "
        "still pass its loader, or nothing is written. Built-ins are read-only. "
        "After writing, the agent registry reloads and `report` says whether the "
        "agent loads and lists the prompt lint's `findings`.\n\n"
        "When to use: for a targeted change to a user agent — fixing a lint "
        "finding, a load error or a critic's concern."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {**_STR, "description": "The user agent's name."},
            "file": {"type": "string", "enum": ["prompt", "config", "spec"]},
            "old_string": {**_STR, "description": "Text that occurs exactly once in the file."},
            "new_string": {**_STR, "description": "The replacement text."},
        },
        "required": ["name", "file", "old_string", "new_string"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": _STR, "description": "The file written."},
            "report": {
                "type": "object",
                "description": "name, kind, load (`ok`/`error`), error, path, findings.",
            },
        },
        "required": ["files", "report"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={
        "name": "always",
        "file": "always",
        "old_string": "visible",
        "new_string": "visible",
    },
    output_visibility={"files": "always", "report": "visible"},
)

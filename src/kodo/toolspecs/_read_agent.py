"""``read_agent`` tool spec — one agent's files, as written.

Agent Builder reads built-ins as examples and user agents before revising them
(doc/AGENT_BUILDER.md §6.2). Built-ins are read-only: only the user agents this
returns can be changed, through ``write_agent`` / ``edit_agent``.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_AGENT"]

_STR = {"type": "string"}

READ_AGENT: ToolSpec = ToolSpec(
    name="read_agent",
    external_name="Read Agent",
    user_description="Read an agent's files",
    description=(
        "Read one agent's files exactly as written: `prompt` is the whole prompt "
        "file (frontmatter included, `{SHARED:…}` tokens unexpanded) and `json` is "
        "its config (a top-level agent's `<name>.json`) or spec (a sub-agent's "
        "`<name>.json`), with `prompt_path` and `json_path`. `frontmatter` is the "
        "frontmatter as the loader resolves it (`tools` sorted, `subagents` in "
        "order); it is empty when the prompt does not parse. `source` is `builtin` "
        "or `user`; built-ins are read-only. `error` is why a broken user agent "
        "does not load, and empty otherwise. Works on broken user agents too, so "
        "they can be fixed.\n\n"
        "When to use: before editing a user agent (to copy `old_string` exactly), "
        "to study a built-in as an example of a pattern, and to read a sub-agent's "
        "spec before writing an agent that calls it."
    ),
    input_schema={
        "type": "object",
        "properties": {"name": {**_STR, "description": "The agent's exact name."}},
        "required": ["name"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "name": _STR,
            "kind": {**_STR, "description": "`agent` or `subagent`."},
            "source": {**_STR, "description": "`builtin` or `user`."},
            "error": {**_STR, "description": "Why it does not load; empty when it does."},
            "prompt_path": _STR,
            "prompt": {**_STR, "description": "The prompt file's whole text."},
            "json_path": _STR,
            "json": {**_STR, "description": "The config or spec file's whole text."},
            "frontmatter": {"type": "object", "description": "The resolved frontmatter."},
        },
        "required": ["name", "kind", "source", "prompt_path", "prompt", "json_path", "json"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"name": "always"},
    output_visibility={
        "name": "hidden",
        "kind": "visible",
        "source": "visible",
        "error": "visible",
        "prompt_path": "visible",
        "prompt": "hidden",
        "json_path": "hidden",
        "json": "hidden",
        "frontmatter": "hidden",
    },
)

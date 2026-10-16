"""``validate_agent`` tool spec — whether one agent loads, and what its lint says.

doc/AGENT_BUILDER.md §5.1 + §5.2, the same report as ``--validate-agent`` and
the Settings panel's Validate button.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["VALIDATE_AGENT"]

_STR = {"type": "string"}

VALIDATE_AGENT: ToolSpec = ToolSpec(
    name="validate_agent",
    external_name="Validate Agent",
    user_description="Validate an agent",
    description=(
        "Report on one agent, built-in or user, loaded or broken. `load` is `ok` or "
        "`error`, and `error` says why it does not load — the agent loader's rules, "
        "the only ones that stop an agent running. `findings` are the prompt lint's "
        "warnings (`rule` `L1`…`L7`, `line` in the prompt file counted from the top, "
        "frontmatter included, or null for something missing, and `message`); "
        "`path` is the prompt file the lines refer to. The lint reads the prompt as "
        "written, before `{SHARED:…}` blocks are expanded.\n\n"
        "When to use: to check an agent that is already written — before reviewing "
        "it, and after changing an agent it depends on."
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
            "load": {**_STR, "description": "`ok` or `error`."},
            "error": {**_STR, "description": "Why it does not load; empty when it does."},
            "path": {**_STR, "description": "The prompt file the line numbers refer to."},
            "findings": {"type": "array", "description": "rule, severity, line, message."},
        },
        "required": ["name", "kind", "load", "error", "path", "findings"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"name": "always"},
    output_visibility={
        "name": "hidden",
        "kind": "hidden",
        "load": "always",
        "error": "always",
        "path": "hidden",
        "findings": "visible",
    },
)

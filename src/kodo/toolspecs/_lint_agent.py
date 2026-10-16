"""``lint_agent`` tool spec — lint a draft prompt before it is written.

doc/AGENT_BUILDER.md §5.2 and §6.2: the cheap pre-check, run on text the
builder holds, so a draft reaches ``write_agent`` with its warnings fixed.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["LINT_AGENT"]

_STR = {"type": "string"}

LINT_AGENT: ToolSpec = ToolSpec(
    name="lint_agent",
    external_name="Lint Agent Draft",
    user_description="Lint a draft agent prompt",
    description=(
        "Run the prompt lint over a draft prompt file without writing it. `kind` "
        "decides which `##` sections the draft needs and, for `subagent`, that it "
        "holds `return_result`; `name` is the name it will be written under, and "
        "the draft's `name:` must equal it. Each finding has `rule` (`L1 "
        "tool-not-granted` … `L7 schema-restated`, explained in the "
        "`kodo-prompt-writing` skill), `line` (counted from the top of the draft, "
        "frontmatter included, or null for something missing) and `message`. An "
        "`error` means the draft does not parse at all. Lint findings are warnings: "
        "an agent with findings still loads.\n\n"
        "When to use: on every draft, before `write_agent`, and on a revised prompt "
        "before writing it back."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["agent", "subagent"]},
            "name": {**_STR, "description": "The name the draft will be written under."},
            "prompt": {**_STR, "description": "The whole draft, frontmatter included."},
        },
        "required": ["kind", "name", "prompt"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "name": _STR,
            "kind": _STR,
            "findings": {"type": "array", "description": "rule, severity, line, message."},
        },
        "required": ["name", "kind", "findings"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"kind": "always", "name": "always", "prompt": "hidden"},
    output_visibility={"name": "hidden", "kind": "hidden", "findings": "visible"},
)

"""``list_tools`` tool spec — the live tool catalog an agent's ``tools:`` can name.

Agent Builder chooses each agent's tools from this list (doc/AGENT_BUILDER.md
D16). The judgment part of choosing — least privilege, which grants travel
together — is the shipped ``kodo-tool-selection`` skill; this is the catalog.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["LIST_TOOLS"]

LIST_TOOLS: ToolSpec = ToolSpec(
    name="list_tools",
    external_name="List Tools",
    user_description="List the tools agents can use",
    description=(
        "List every tool an agent's frontmatter `tools:` can grant, as this Kōdo "
        "build defines them. Each row has `name` (what `tools:` lists), "
        "`description` (the text the agent holding it reads, ending with when to "
        "use it), `security_impact` (`None` … `Critical`, the level Kōdo's security "
        "layer and the user's posture use to decide which calls ask the user first), "
        "`autonomous_mode` (`unavailable` — withheld while the user is away; `auto-accepted` — the "
        "user's answer is synthesized; empty — no difference), `requires_project` "
        "(refused until a project is open) and `modifies_files` (granting it "
        "requires `{SHARED:editing}` in the prompt). `return_result` is not listed: "
        "every sub-agent is granted it automatically. `run_subagent` stands for one "
        "`run_subagent_<name>` tool per entry in the agent's `subagents:`.\n\n"
        "When to use: before writing an agent's `tools:` list, and when a review "
        "needs to check what a granted tool does."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "description": "One row per tool: name, description, security_impact, "
                "autonomous_mode, requires_project, modifies_files.",
            },
        },
        "required": ["tools"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={},
    output_visibility={"tools": "hidden"},
)

"""``list_agents`` tool spec — every built-in and user agent Agent Builder can see.

The builder's survey call (doc/AGENT_BUILDER.md §6.2): what exists, what each
agent delegates to, which built-ins can be copied, and which user agents are
broken and why.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["LIST_AGENTS"]

LIST_AGENTS: ToolSpec = ToolSpec(
    name="list_agents",
    external_name="List Agents",
    user_description="List Kōdo's agents",
    description=(
        "List every agent the agent registry knows: the built-in agents Kōdo ships "
        "(names starting `kodo_`) and the user agents under the user agents root, "
        "loaded or broken. Each row has `name`, `kind` (`agent` for a top-level agent "
        "the user picks, `subagent` for one another agent delegates to), `source` "
        "(`builtin` or `user`), `label`, a one-line `description`, `subagents` (the "
        "agents it can spawn, in order), `critic`, `role` (`critic` for a reviewer), "
        "`standalone`, `copyable` (a built-in `copy_builtin_agent` accepts as a "
        "source) and `error` (why a broken user agent does not load; empty "
        "otherwise). Top-level rows add `selectable` and `interactive`. `root` is the "
        "user agents root.\n\n"
        "When to use: at the start of a build, to find a built-in to copy or read as "
        "an example and to avoid names already taken; again whenever you need the "
        "current set."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "agents": {"type": "array", "description": "One row per agent, loaded or broken."},
            "root": {"type": "string", "description": "The user agents root."},
        },
        "required": ["agents", "root"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={},
    output_visibility={"agents": "hidden", "root": "visible"},
)

"""``copy_builtin_agent`` tool spec — copy a built-in, with its pipeline, as a user agent.

doc/AGENT_BUILDER.md §3 and §6.2: the same deep copy as the CLI's
``--copy-agent`` and the Settings panel's "Copy a built-in agent…", so a build
can start from a working pipeline instead of a blank page.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["COPY_BUILTIN_AGENT"]

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}

COPY_BUILTIN_AGENT: ToolSpec = ToolSpec(
    name="copy_builtin_agent",
    external_name="Copy Built-in Agent",
    user_description="Copy a built-in agent",
    description=(
        "Copy a built-in agent into the user agents root together with its whole "
        "pipeline: every agent in its `subagents:` and every `critic:`, repeated "
        "until nothing new is added. `prefix` replaces `kodo_` on every copied name "
        "(`acme_` turns `kodo_guide` into `acme_guide` and `kodo_coder` into "
        "`acme_coder`), in the file names, the frontmatter, the config or spec, and "
        "every whole-word mention in the prompt bodies; labels get `(acme)` "
        "appended. The prefix starts with a lowercase letter, holds lowercase "
        "letters, digits and underscores, and never starts `kodo_`. Only built-ins "
        "whose `list_agents` row says `copyable: true` can be copied.\n\n"
        "When a copied name is already taken and `replace` is omitted, nothing is "
        "written: the result has `done: false`, the `members` it would copy and a "
        "`conflicts` report. Ask the user, then call again with `replace: true` "
        "(overwrite the taken names) or `replace: false` (keep them, copy the "
        "rest). Otherwise the copy is written all-or-nothing and the result lists "
        "`copied`, `kept`, and `failed` (each copied member that does not load, "
        "with why).\n\n"
        "When to use: when the agent to build is a variation of a built-in "
        "pipeline; edit the copies afterwards with `edit_agent`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {**_STR, "description": "The built-in to copy, e.g. `kodo_problem_solver`."},
            "prefix": {**_STR, "description": "Replaces `kodo_`, e.g. `acme_`."},
            "replace": {
                "type": "boolean",
                "description": "The user's answer for names already taken: true replaces "
                "them, false keeps them. Omit until the user has answered.",
            },
        },
        "required": ["name", "prefix"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "done": {
                "type": "boolean",
                "description": "False when names are taken and `replace` was omitted.",
            },
            "members": {
                "type": "array",
                "description": "Every member: source, name, kind.",
            },
            "conflicts": {**_STR, "description": "The taken names, when `done` is false."},
            "copied": _STR_LIST,
            "kept": _STR_LIST,
            "failed": {**_STR_LIST, "description": "`name: reason` per copy that does not load."},
        },
        "required": ["done", "members", "copied", "kept", "failed"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"name": "always", "prefix": "always", "replace": "always"},
    output_visibility={
        "done": "hidden",
        "members": "hidden",
        "conflicts": "visible",
        "copied": "always",
        "kept": "visible",
        "failed": "always",
    },
)

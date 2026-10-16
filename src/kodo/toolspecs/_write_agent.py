"""``write_agent`` tool spec — create or overwrite one user agent.

doc/AGENT_BUILDER.md §6.2. The service builds the paths from ``kind`` and
``name``, so the agent never names a file; every file passes its loader before
it is written, and the result is the agent's validation report.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["WRITE_AGENT"]

_STR = {"type": "string"}

WRITE_AGENT: ToolSpec = ToolSpec(
    name="write_agent",
    external_name="Write Agent",
    user_description="Write a user agent",
    description=(
        "Create a user agent, or overwrite one, as two files: the prompt and its "
        "JSON. `kind` `agent` writes a top-level agent (`agent_<name>.md` + its "
        "`<name>.json` config) under the user agents root; `subagent` writes a "
        "sub-agent (`subagent_<name>.md` + its `<name>.json` spec) under its "
        "`subagents/` directory. `prompt` is the whole prompt file, frontmatter "
        "included, and its `name:` must equal `name`. `config` is the config or "
        'spec object; its `"name"` is filled in when absent.\n\n'
        "Refused, with nothing written: a name starting `kodo_` (built-ins are "
        "read-only), the name `subagents`, a name that is not lowercase letters, "
        "digits and underscores starting with a letter, a name the other kind "
        'already uses, a file its loader rejects, and `"default"` or '
        '`"interactive"` in a config. After writing, the agent registry reloads '
        "and `report` says whether the agent loads (`load`, `error`) and lists the "
        "prompt lint's `findings`. A load error can be cross-agent — a "
        "`subagents:` entry not written yet — and clears once that agent exists.\n\n"
        "When to use: to write a new agent, or to replace most of an existing user "
        "agent; for a targeted change use `edit_agent`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["agent", "subagent"]},
            "name": {**_STR, "description": "The agent's name, e.g. `acme_reviewer`."},
            "prompt": {**_STR, "description": "The whole prompt file, frontmatter included."},
            "config": {
                "type": "object",
                "description": "A top-level agent's config, or a sub-agent's spec.",
            },
        },
        "required": ["kind", "name", "prompt", "config"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": _STR, "description": "The files written."},
            "report": {
                "type": "object",
                "description": "name, kind, load (`ok`/`error`), error, path, findings.",
            },
        },
        "required": ["files", "report"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"kind": "always", "name": "always", "prompt": "hidden", "config": "hidden"},
    output_visibility={"files": "always", "report": "visible"},
)

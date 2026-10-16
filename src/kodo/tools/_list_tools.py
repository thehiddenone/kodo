"""``list_tools`` tool — the live tool catalog an agent's ``tools:`` can name.

Dispatch handler for :data:`kodo.toolspecs.LIST_TOOLS`. Unlike the other
builder tools it needs no service: the catalog is :data:`kodo.toolspecs.ALL_TOOLS`,
which this tier may import, and it is the same list the agent loader validates
every ``tools:`` entry against.
"""

from __future__ import annotations

import json

from kodo.toolspecs import ALL_TOOLS, RETURN_RESULT

from ._tool import Tool

__all__ = ["ListToolsTool"]


class ListToolsTool(Tool):
    """List every grantable tool with what an agent author needs to choose it."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        # `return_result` is granted to every sub-agent by the registry, never
        # declared, so offering it would only invite a redundant grant.
        rows = [
            {
                "name": spec.name,
                "description": spec.description,
                "security_impact": spec.security_impact.label,
                "autonomous_mode": spec.autonomous_mode or "",
                "requires_project": spec.requires_project,
                "modifies_files": spec.modifies_files,
            }
            for spec in sorted(ALL_TOOLS, key=lambda s: s.name)
            if spec.name != RETURN_RESULT.name
        ]
        return json.dumps({"tools": rows}, ensure_ascii=False)

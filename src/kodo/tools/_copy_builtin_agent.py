"""``copy_builtin_agent`` tool — copy a built-in, with its pipeline, as a user agent.

Dispatch handler for :data:`kodo.toolspecs.COPY_BUILTIN_AGENT`. The engine's
:class:`~kodo.tools.AgentWorkshopLike` service plans, refuses and writes; this
wrapper only checks the argument types.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["CopyBuiltinAgentTool"]


class CopyBuiltinAgentTool(Tool):
    """Copy one built-in agent and its closure under a new prefix."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = tool_input.get("name")
        prefix = tool_input.get("prefix")
        replace = tool_input.get("replace")
        if not isinstance(name, str) or not name.strip():
            return json.dumps({"error": "copy_builtin_agent requires a non-empty 'name'."})
        if not isinstance(prefix, str) or not prefix.strip():
            return json.dumps({"error": "copy_builtin_agent requires a non-empty 'prefix'."})
        if replace is not None and not isinstance(replace, bool):
            return json.dumps({"error": "'replace' must be true, false, or omitted."})
        try:
            result = await self.context.services.agent_workshop().copy_builtin(
                name.strip(), prefix.strip(), replace
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)

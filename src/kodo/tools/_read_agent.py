"""``read_agent`` tool — one agent's files, as written.

Dispatch handler for :data:`kodo.toolspecs.READ_AGENT`, a thin wrapper over
the engine's :class:`~kodo.tools.AgentWorkshopLike` service.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ReadAgentTool"]


class ReadAgentTool(Tool):
    """Read one agent's prompt and config or spec."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = tool_input.get("name")
        if not isinstance(name, str) or not name.strip():
            return json.dumps({"error": "read_agent requires a non-empty 'name'."})
        try:
            result = await self.context.services.agent_workshop().read_agent(name.strip())
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)

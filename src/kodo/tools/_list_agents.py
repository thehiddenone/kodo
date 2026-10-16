"""``list_agents`` tool — every built-in and user agent.

Dispatch handler for :data:`kodo.toolspecs.LIST_AGENTS`, a thin wrapper over
the engine's :class:`~kodo.tools.AgentWorkshopLike` service.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ListAgentsTool"]


class ListAgentsTool(Tool):
    """List every agent the registry knows, loaded or broken."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        try:
            result = await self.context.services.agent_workshop().list_agents()
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)

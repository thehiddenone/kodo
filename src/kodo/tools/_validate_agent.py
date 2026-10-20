"""``validate_agent`` tool — whether one agent loads, and what its lint says.

Dispatch handler for :data:`kodo.toolspecs.VALIDATE_AGENT`, a thin wrapper over
the engine's :class:`~kodo.tools.AgentWorkshopLike` service.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ValidateAgentTool"]


class ValidateAgentTool(Tool):
    """Report on one agent's load check and lint."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = tool_input.get("name")
        if not isinstance(name, str) or not name.strip():
            return json.dumps({"error": "validate_agent requires a non-empty 'name'."})
        try:
            result = await self.context.services.agent_workshop().validate_agent(name.strip())
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        # The workshop reports an agent that loads with an empty ``error``; a
        # top-level ``error`` key marks the call itself as failed, so drop it.
        if not result.get("error"):
            result = {key: value for key, value in result.items() if key != "error"}
        return json.dumps(result, ensure_ascii=False)

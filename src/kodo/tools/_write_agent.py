"""``write_agent`` tool — create or overwrite one user agent.

Dispatch handler for :data:`kodo.toolspecs.WRITE_AGENT`. The engine's
:class:`~kodo.tools.AgentWorkshopLike` service builds the paths, checks each
file with its loader and reports on the result; this wrapper only checks the
argument types.
"""

from __future__ import annotations

import json
from typing import cast

from ._tool import Tool

__all__ = ["WriteAgentTool"]


class WriteAgentTool(Tool):
    """Write one user agent's prompt and config or spec."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        kind = tool_input.get("kind")
        name = tool_input.get("name")
        prompt = tool_input.get("prompt")
        config = tool_input.get("config")
        for key, value in (("kind", kind), ("name", name), ("prompt", prompt)):
            if not isinstance(value, str) or not value.strip():
                return json.dumps({"error": f"write_agent requires a non-empty {key!r}."})
        if not isinstance(config, dict):
            return json.dumps({"error": "write_agent requires 'config' to be a JSON object."})
        try:
            result = await self.context.services.agent_workshop().write_agent(
                cast(str, kind).strip(),
                cast(str, name).strip(),
                cast(str, prompt),
                cast(dict[str, object], config),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)

"""``lint_agent`` tool — lint a draft prompt before it is written.

Dispatch handler for :data:`kodo.toolspecs.LINT_AGENT`, a thin wrapper over the
engine's :class:`~kodo.tools.AgentWorkshopLike` service.
"""

from __future__ import annotations

import json
from typing import cast

from ._tool import Tool

__all__ = ["LintAgentTool"]


class LintAgentTool(Tool):
    """Lint one draft prompt against the prompt-writing rules."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        kind = tool_input.get("kind")
        name = tool_input.get("name")
        prompt = tool_input.get("prompt")
        for key, value in (("kind", kind), ("name", name), ("prompt", prompt)):
            if not isinstance(value, str) or not value.strip():
                return json.dumps({"error": f"lint_agent requires a non-empty {key!r}."})
        try:
            result = await self.context.services.agent_workshop().lint_agent(
                cast(str, kind).strip(), cast(str, name).strip(), cast(str, prompt)
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)

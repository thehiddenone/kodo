"""``edit_agent`` tool — one exact-string edit to one user agent file.

Dispatch handler for :data:`kodo.toolspecs.EDIT_AGENT`. The engine's
:class:`~kodo.tools.AgentWorkshopLike` service finds the file, applies the
edit, checks it with its loader and reports on the result; this wrapper only
checks the argument types.
"""

from __future__ import annotations

import json
from typing import cast

from ._tool import Tool

__all__ = ["EditAgentTool"]


class EditAgentTool(Tool):
    """Replace one exact string in one user agent file."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = tool_input.get("name")
        file = tool_input.get("file")
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        for key, value in (("name", name), ("file", file), ("old_string", old_string)):
            if not isinstance(value, str) or not value:
                return json.dumps({"error": f"edit_agent requires a non-empty {key!r}."})
        if not isinstance(new_string, str):
            return json.dumps({"error": "edit_agent requires 'new_string' (it can be empty)."})
        try:
            result = await self.context.services.agent_workshop().edit_agent(
                cast(str, name).strip(), cast(str, file), cast(str, old_string), new_string
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)

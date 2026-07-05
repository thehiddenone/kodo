"""``update_web_search_state`` tool — write to the web_search agent's TTL memory.

Dispatch handler for :data:`kodo.toolspecs.UPDATE_WEB_SEARCH_STATE`. A thin
wrapper over :class:`kodo.websearch.WebSearchStateStore` — see
doc/WEB_SEARCH.md for the full pacing/bookkeeping protocol.
"""

from __future__ import annotations

import json

from kodo.project import kodo_user_dir
from kodo.websearch import WebSearchStateStore

from ._tool import Tool

__all__ = ["UpdateWebSearchStateTool"]


class UpdateWebSearchStateTool(Tool):
    """Set, delete, or time-mark one key in the agent's key-value memory."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        key = tool_input.get("key")
        if not key or not isinstance(key, str):
            return json.dumps({"error": "update_web_search_state requires a non-empty 'key'."})
        value = tool_input.get("value")
        if not isinstance(value, str):
            return json.dumps({"error": "update_web_search_state requires a string 'value'."})

        store = WebSearchStateStore(kodo_user_dir() / "websearch" / "agent_state.json")
        store.update(key, value)
        return json.dumps({"status": "ok"})

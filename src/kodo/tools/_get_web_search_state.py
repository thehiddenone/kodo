"""``get_web_search_state`` tool — read the web_search agent's TTL memory.

Dispatch handler for :data:`kodo.toolspecs.GET_WEB_SEARCH_STATE`. A thin
wrapper over :class:`kodo.websearch.WebSearchStateStore` — see
doc/WEB_SEARCH.md for the full pacing/bookkeeping protocol.
"""

from __future__ import annotations

import json

from kodo.project import kodo_user_dir
from kodo.websearch import WebSearchStateStore

from ._tool import Tool

__all__ = ["GetWebSearchStateTool"]


class GetWebSearchStateTool(Tool):
    """Return the agent's full key-value memory, expired entries evicted."""

    async def handle(self, tool_input: dict[str, object]) -> str:  # noqa: ARG002
        store = WebSearchStateStore(kodo_user_dir() / "websearch" / "agent_state.json")
        return json.dumps({"state": store.get_all()})

"""``web_search`` tool — external web search (placeholder).

Dispatch handler for :data:`kodo.toolspecs.WEB_SEARCH`. The real integration
(DuckDuckGo public API) is a deferred follow-up; until then this returns a
schema-compliant envelope with an empty ``results`` list and an explanatory
``note`` so the Investigator can degrade gracefully to code exploration.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["WebSearchTool"]

_log = logging.getLogger(__name__)

_UNWIRED_NOTE = (
    "Web search is not yet connected to a provider (DuckDuckGo wiring is a "
    "pending follow-up); no results are available. Rely on code exploration "
    "for now."
)


class WebSearchTool(Tool):
    """Return web search results for a query (currently a placeholder)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        query = tool_input.get("query")
        if not query or not isinstance(query, str):
            return json.dumps({"error": "web_search requires a non-empty 'query'."})
        _log.info("web_search (placeholder) from %s: %s", self.context.agent_name, query)
        # Placeholder: no provider is wired yet. Return the empty, compliant shape.
        return json.dumps({"results": [], "note": _UNWIRED_NOTE})

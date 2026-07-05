"""``remaining_time`` tool — the web_search agent's timeout countdown (doc/WEB_SEARCH.md).

Dispatch handler for :data:`kodo.toolspecs.REMAINING_TIME`. Reads the run's
:attr:`~kodo.tools.ToolContext.deadline`, set by the engine from the
``web_search`` tool's caller-supplied, 600s-capped ``timeout``.
"""

from __future__ import annotations

import json
import time

from ._tool import Tool

__all__ = ["RemainingTimeTool"]


class RemainingTimeTool(Tool):
    """Report how many seconds remain before this run's deadline."""

    async def handle(self, tool_input: dict[str, object]) -> str:  # noqa: ARG002
        # No deadline should never happen in practice (only the web_search
        # agent holds this tool, and the engine always sets one for it) --
        # fail closed (0 remaining) rather than imply unlimited time.
        remaining = max(0.0, self.context.deadline - time.time()) if self.context.deadline else 0.0
        return json.dumps({"remaining_seconds": remaining})

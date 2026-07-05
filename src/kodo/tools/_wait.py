"""``wait`` tool — pause to avoid bursting search-engine requests (doc/WEB_SEARCH.md).

Dispatch handler for :data:`kodo.toolspecs.WAIT`. A plain, clamped sleep —
never sleeps past the run's :attr:`~kodo.tools.ToolContext.deadline` (if one
is set), so a generous ``wait`` call can't itself eat the time budget meant
for producing a report.
"""

from __future__ import annotations

import asyncio
import json
import time

from ._tool import Tool

__all__ = ["WaitTool"]

_DEFAULT_SECONDS = 5.0
_MAX_SECONDS = 30.0


class WaitTool(Tool):
    """Sleep for a clamped duration and return nothing else."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        raw = tool_input.get("seconds")
        seconds = float(raw) if isinstance(raw, (int, float)) and raw > 0 else _DEFAULT_SECONDS
        seconds = min(seconds, _MAX_SECONDS)
        if self.context.deadline is not None:
            seconds = max(0.0, min(seconds, self.context.deadline - time.time()))
        await asyncio.sleep(seconds)
        return json.dumps({})

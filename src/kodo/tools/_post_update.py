"""``post_update`` tool — sends a non-blocking progress update to the UI."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["PostUpdateTool"]

_log = logging.getLogger(__name__)


class PostUpdateTool(Tool):
    """Send a progress update to the client UI."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        message = str(tool_input.get("message", ""))
        _log.info("post_update: %r", message[:120])
        await self.context.services.post_update(message)
        return json.dumps({"status": "posted"})

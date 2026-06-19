"""``disable_autonomous_mode`` tool — turns off autonomous mode."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["DisableAutonomousModeTool"]

_log = logging.getLogger(__name__)


class DisableAutonomousModeTool(Tool):
    """Turn off autonomous mode and notify the client."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        reason = str(tool_input.get("reason", ""))
        _log.info("disable_autonomous_mode: reason=%r", reason)
        await self.context.services.disable_autonomous_mode()
        return json.dumps({"status": "disabled"})

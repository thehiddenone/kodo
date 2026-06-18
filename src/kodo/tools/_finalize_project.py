"""``finalize_project`` tool — marks the session done."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["FinalizeProjectTool"]

_log = logging.getLogger(__name__)


class FinalizeProjectTool(Tool):
    """Mark the project finalized; the worker exits after this turn."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        self.context.session.phase = "done"
        _log.info("finalize_project: session marked done")
        return json.dumps({"status": "done"})

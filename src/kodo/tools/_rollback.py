"""``rollback`` tool — invokes the injected rollback procedure."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["RollbackTool"]

_log = logging.getLogger(__name__)


class RollbackTool(Tool):
    """Roll the mirror back to ``target_sha`` and rebuild session state."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        target_sha = str(tool_input.get("target_sha", "")).strip()
        if not target_sha:
            return json.dumps({"error": "target_sha is required"})
        _log.info("rollback: target_sha=%s", target_sha[:12])
        await self.context.services.rollback(target_sha)
        return json.dumps({"status": "completed"})

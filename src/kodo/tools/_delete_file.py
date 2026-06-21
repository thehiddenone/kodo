"""``delete_file`` tool — removes a file inside the project root."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["DeleteFileTool"]

_log = logging.getLogger(__name__)


class DeleteFileTool(Tool):
    """Delete an existing file (fails if it does not exist)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        try:
            target = ctx.resolver.resolve(path)
            if not target.exists():
                raise FileNotFoundError(f"File not found: {path!r}")
            target.unlink()
        except OSError as exc:
            _log.info("delete_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "deleted", "path": path})

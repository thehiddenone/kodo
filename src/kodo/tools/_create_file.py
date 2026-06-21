"""``create_file`` tool — writes a new file inside the project root."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["CreateFileTool"]

_log = logging.getLogger(__name__)


class CreateFileTool(Tool):
    """Create a new file (fails if it already exists)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))
        try:
            target = ctx.resolver.resolve(path)
            if target.exists():
                raise FileExistsError(f"File already exists: {path!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            _log.info("create_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "created", "path": path})

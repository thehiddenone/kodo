"""``edit_file`` tool — overwrites an existing file inside the project root."""

from __future__ import annotations

import json
import logging

from ._paths import resolve_within
from ._tool import Tool

__all__ = ["EditFileTool"]

_log = logging.getLogger(__name__)


class EditFileTool(Tool):
    """Overwrite an existing file's contents (fails if it does not exist)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))
        try:
            target = resolve_within(ctx.workspace.project_root, path)
            if not target.exists():
                raise FileNotFoundError(f"File not found: {path!r}")
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            _log.info("edit_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "edited", "path": path})

"""``copy_file`` tool — copies a file within the project root."""

from __future__ import annotations

import json
import logging
import shutil

from ._tool import Tool

__all__ = ["CopyFileTool"]

_log = logging.getLogger(__name__)


class CopyFileTool(Tool):
    """Copy a file from *source* to *destination* (both inside the root)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        source = str(tool_input.get("source", ""))
        destination = str(tool_input.get("destination", ""))
        try:
            src = ctx.resolver.resolve(source)
            dst = ctx.resolver.resolve(destination)
            if not src.exists():
                raise FileNotFoundError(f"Source not found: {source!r}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            _log.info("copy_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "copied", "source": source, "destination": destination})

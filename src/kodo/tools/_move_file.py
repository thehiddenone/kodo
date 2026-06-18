"""``move_file`` tool — moves/renames a file within the project root."""

from __future__ import annotations

import json
import logging
import shutil

from ._paths import resolve_within
from ._tool import Tool

__all__ = ["MoveFileTool"]

_log = logging.getLogger(__name__)


class MoveFileTool(Tool):
    """Move a file from *source* to *destination* (both inside the root)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        source = str(tool_input.get("source", ""))
        destination = str(tool_input.get("destination", ""))
        try:
            src = resolve_within(ctx.workspace.project_root, source)
            dst = resolve_within(ctx.workspace.project_root, destination)
            if not src.exists():
                raise FileNotFoundError(f"Source not found: {source!r}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dst)
        except OSError as exc:
            _log.info("move_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "moved", "source": source, "destination": destination})

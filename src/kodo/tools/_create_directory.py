"""``create_directory`` tool — directory creation inside the project root.

Creates a directory, including any missing parents (``mkdir -p`` semantics),
succeeding if it already exists. Split out of the former ``filesystem`` tool's
``create_dir`` operation; deleting, copying, or moving whole files or
directories stays in :class:`~kodo.tools._filesystem.FilesystemTool`.

``temporary: true`` resolves ``path`` under the session's private scratch
directory instead (see :meth:`~kodo.tools.Tool.resolve_path`).
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["CreateDirectoryTool"]

_log = logging.getLogger(__name__)


class CreateDirectoryTool(Tool):
    """Create a directory, including any missing parents."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        temporary = bool(tool_input.get("temporary", False))

        try:
            target = self.resolve_path(path, temporary=temporary)
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.info("create_directory from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        return json.dumps({"status": "created", "path": path})

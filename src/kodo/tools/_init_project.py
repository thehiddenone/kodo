"""``init_project`` tool — augment an existing directory with Kodo's git mirror.

The filesystem work (rejecting a directory that already has ``.kodo/``,
judging whether the directory is empty, laying out
``specs/``/``src/``/``test/``/``.kodo/``/``kodo.md`` only when it is) and the
checkpoint-mirror + client round-trip (asking the extension to add the folder
to the open VS Code workspace, when it isn't already there) both live *above*
this package in the import graph — they touch the engine's
:class:`~kodo.runtime.SessionWorkspace`, ``RootMirrorManager`` and the message
sink. So this handler is a thin shim that delegates to
``EngineServices.init_project`` and formats the result.
"""

from __future__ import annotations

import json
import logging

from kodo.project import ProjectLayoutError

from ._tool import Tool

__all__ = ["InitProjectTool"]

_log = logging.getLogger(__name__)


class InitProjectTool(Tool):
    """Bring an existing directory under Kodo's project layout + git mirror."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        raw_path = tool_input.get("path")
        path = str(raw_path).strip() if isinstance(raw_path, str) else ""
        if not path:
            return json.dumps({"error": "init_project requires a non-empty 'path'."})
        try:
            result = await self.context.services.init_project(path)
        except ProjectLayoutError as exc:
            return json.dumps({"error": str(exc)})
        _log.info(
            "init_project by %s: path=%r -> %s (scaffolded=%s)",
            self.context.agent_name,
            path,
            result.get("path"),
            result.get("scaffolded"),
        )
        return json.dumps(
            {
                "path": result["path"],
                "name": result["name"],
                "scaffolded": result["scaffolded"],
            }
        )

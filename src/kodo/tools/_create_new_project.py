"""``create_new_project`` tool — scaffold a new project and add it to the workspace.

The filesystem work (slugifying the name, creating the directory under the
workspace root, laying out ``specs/``/``src/``/``test/``/``.kodo/``/``kodo.md``
+ the checkpoint mirror) and the client round-trip (asking the extension to add
the folder to the open VS Code workspace) both live *above* this package in the
import graph — they touch the engine's :class:`~kodo.runtime.SessionWorkspace`,
``RootMirrorManager`` and the message sink. So this handler is a thin shim that
delegates to ``EngineServices.create_project`` and formats the result.
"""

from __future__ import annotations

import json
import logging

from kodo.project import ProjectLayoutError

from ._tool import Tool

__all__ = ["CreateNewProjectTool"]

_log = logging.getLogger(__name__)


class CreateNewProjectTool(Tool):
    """Create a new project directory and register it with the workspace."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", "")).strip()
        raw_path = tool_input.get("path")
        path = str(raw_path).strip() if isinstance(raw_path, str) else ""
        if not name and not path:
            return json.dumps(
                {"error": "create_new_project requires a non-empty 'name' or 'path'."}
            )
        try:
            result = await self.context.services.create_project(name, path or None)
        except ProjectLayoutError as exc:
            return json.dumps({"error": str(exc)})
        _log.info(
            "create_new_project by %s: name=%r path=%r -> %s",
            self.context.agent_name,
            name,
            path,
            result.get("path"),
        )
        return json.dumps({"path": result["path"], "name": result["name"]})

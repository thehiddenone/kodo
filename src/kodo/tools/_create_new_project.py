"""``create_new_project`` tool — scaffold a new project and add it to the workspace.

The filesystem work (slugifying the name, creating the directory under the
workspace root, laying out ``specs/``/``src/``/``test/``/``.kodo/``/``kodo.md``
+ the checkpoint mirror) and the client round-trip (asking the extension to add
the folder to the open VS Code workspace) both live *above* this package in the
import graph — they touch the engine's :class:`~kodo.runtime.SessionWorkspace`,
``RootMirrorManager`` and the message sink. So this handler is a thin shim that
delegates to ``EngineServices.create_project``/``bootstrap_project`` and
formats the result.

The tool takes no ``path`` — the agent only ever supplies a human-readable
``name``; ``EngineServices.create_project``/``bootstrap_project`` alone decide
the on-disk directory (a real absolute ``path`` still exists as an engine-side
parameter, fed only by the native "Create Project" folder-picker dialog,
never by this tool). When the agent calls this with no project/workspace
existing yet — regardless of whether ``name`` was given, since a homeless
session has nowhere to place *any* name until a workspace root is resolved —
it delegates to ``EngineServices.bootstrap_project`` instead: an "open
directory" folder-picker dialog in interactive sessions, an automatic
``~/kodo-projects/<name>`` directory in autonomous ones (doc/WS_PROTOCOL.md
§6.6). If a workspace already exists, creating an *additional* project still
requires a non-empty ``name``.
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
        try:
            if not self.context.has_workspace:
                result = await self.context.services.bootstrap_project(name)
            else:
                if not name:
                    return json.dumps({"error": "create_new_project requires a non-empty 'name'."})
                result = await self.context.services.create_project(name)
        except ProjectLayoutError as exc:
            return json.dumps({"error": str(exc)})
        if "error" in result:
            return json.dumps({"error": result["error"]})
        _log.info(
            "create_new_project by %s: name=%r -> %s",
            self.context.agent_name,
            name,
            result.get("path"),
        )
        return json.dumps({"path": result["path"], "name": result["name"]})

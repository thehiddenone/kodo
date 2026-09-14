"""``scaffold_new_project`` tool — set up a directory as a Kodo project.

Merges the former ``create_new_project`` and ``init_project`` handlers into
one: which underlying engine primitive runs is decided entirely by the
``path`` input.

* ``path`` given: delegates to ``EngineServices.init_project`` (the former
  ``init_project`` tool's behavior) — *path* must already exist; if it's
  already a Kodo project (``.kodo/`` present), the engine no-ops and reports
  ``already_scaffolded`` rather than erroring.
* No ``path``, no workspace bound yet: delegates to
  ``EngineServices.bootstrap_project`` (the former ``create_new_project``
  bootstrap fork), regardless of whether ``name`` was given.
* No ``path``, workspace already bound: delegates to
  ``EngineServices.create_project`` and requires a non-empty ``name``.

The filesystem work and client round-trip for all three both live *above*
this package in the import graph (the engine's
:class:`~kodo.runtime.SessionWorkspace`, ``RootMirrorManager`` and the
message sink) — this handler is a thin shim that delegates and formats the
result. See :mod:`kodo.toolspecs._scaffold_new_project` for the full
behavior contract.

All three engine primitives are bound with ``wait_for_attach=True``, so each
of them blocks until the VS Code window confirms the new directory is really
one of its workspace folders — across the window reload that adding it can
trigger (``EngineCore._attach_folder``). That makes this tool call slow in
exactly the case where returning early used to leave the agent poking at a
workspace mid-restart. A confirmation that fails or times out is reported as
``workspace_attached: false`` plus a ``warning``, never as an error: the
project genuinely exists on disk and is bound to the session either way, and
an agent told "error" would scaffold it a second time.
"""

from __future__ import annotations

import json
import logging

from kodo.project import ProjectLayoutError

from ._tool import Tool

__all__ = ["ScaffoldNewProjectTool"]

_log = logging.getLogger(__name__)


class ScaffoldNewProjectTool(Tool):
    """Set up a directory — new or existing — as a Kodo project."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        raw_path = tool_input.get("path")
        path = str(raw_path).strip() if isinstance(raw_path, str) else ""
        name = str(tool_input.get("name", "")).strip()
        try:
            if path:
                result = await self.context.services.init_project(path)
            elif not self.context.has_workspace:
                result = await self.context.services.bootstrap_project(name)
            else:
                if not name:
                    return json.dumps(
                        {"error": "scaffold_new_project requires a non-empty 'name' or 'path'."}
                    )
                result = await self.context.services.create_project(name)
        except ProjectLayoutError as exc:
            return json.dumps({"error": str(exc)})
        if "error" in result:
            return json.dumps({"error": result["error"]})
        _log.info(
            "scaffold_new_project by %s: path=%r name=%r -> %s (workspace_attached=%s)",
            self.context.agent_name,
            path,
            name,
            result.get("path"),
            result.get("workspace_attached", True),
        )
        payload: dict[str, object] = {
            "path": result["path"],
            "name": result["name"],
            "scaffolded": result.get("scaffolded", True),
            "already_scaffolded": result.get("already_scaffolded", False),
            "workspace_attached": result.get("workspace_attached", True),
        }
        warning = result.get("warning")
        if warning:
            payload["warning"] = warning
        return json.dumps(payload)

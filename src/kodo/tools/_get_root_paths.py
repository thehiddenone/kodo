"""``get_root_paths`` tool — list the filesystem roots the agent works within.

Returns the mode-aware root list the engine attached to this run's context: the
single bound project root in Guided mode, or every open VS Code workspace folder
in Problem Solver mode. The list is already synced from the extension over the
WS protocol, so the tool is a pure read of context state.

``temporary: true`` instead returns a single root for this run's private
scratch directory (``kodo.project.session_temp_dir``) — the same directory the
native file tools resolve into with their own ``temporary: true`` (see
:meth:`~kodo.tools.Tool.resolve_path`) and that ``run_command`` accepts as an
absolute ``working_dir`` (see ``kodo.tools.ProjectPathResolver``'s
``extra_roots``). The directory is created eagerly here so it is guaranteed to
exist before the agent uses the path.
"""

from __future__ import annotations

import json
import logging

from kodo.project import session_temp_dir

from ._tool import Tool

__all__ = ["GetRootPathsTool"]

_log = logging.getLogger(__name__)


class GetRootPathsTool(Tool):
    """Report the project root path(s) for the current workspace."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        if bool(tool_input.get("temporary", False)):
            scratch_dir = session_temp_dir(self.context.session_id)
            scratch_dir.mkdir(parents=True, exist_ok=True)
            roots = [{"name": "scratch", "path": str(scratch_dir)}]
        else:
            roots = [{"name": r.name, "path": r.path} for r in self.context.root_paths]
        _log.info("get_root_paths from %s: %d root(s)", self.context.agent_name, len(roots))
        return json.dumps({"roots": roots})

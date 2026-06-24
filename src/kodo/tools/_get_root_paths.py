"""``get_root_paths`` tool — list the filesystem roots the agent works within.

Returns the mode-aware root list the engine attached to this run's context: the
single bound project root in Guided mode, or every open VS Code workspace folder
in Problem Solver mode. The list is already synced from the extension over the
WS protocol, so the tool is a pure read of context state.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["GetRootPathsTool"]

_log = logging.getLogger(__name__)


class GetRootPathsTool(Tool):
    """Report the project root path(s) for the current workspace."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        roots = [{"name": r.name, "path": r.path} for r in self.context.root_paths]
        _log.info("get_root_paths from %s: %d root(s)", self.context.agent_name, len(roots))
        return json.dumps({"roots": roots})

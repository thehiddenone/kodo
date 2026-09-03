"""``guided_dev_status`` tool — scans tracked documents' status (Guided mode only)."""

from __future__ import annotations

import json
from pathlib import Path

from kodo.guided_state import scan_tracked_files

from ._document_status import status_from_state
from ._tool import Tool

__all__ = ["GuidedDevStatusTool"]


class GuidedDevStatusTool(Tool):
    """Report every tracked document's current status, across every bound project.

    The walk itself only reads the project-scoped evolution logs; each row's
    status is then merged with that document's session-scoped findings backlog
    by :func:`~kodo.tools.status_from_state` (doc/FINDINGS.md §6), which is the
    same seam the engine's review loop uses.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        if ctx.mode != "guided":
            return json.dumps({"error": "guided_dev_status is only available in Guided mode."})
        roots = ctx.root_paths
        if not roots:
            return json.dumps({"error": "No project is bound."})
        files: list[dict[str, object]] = []
        for root in roots:
            for entry in scan_tracked_files(Path(root.path)):
                logical = f"{root.name}/{entry['path']}"
                files.append(
                    {
                        "path": logical,
                        "status": status_from_state(entry, ctx.findings_dir, logical),
                        "last_event": entry["last_event"],
                    }
                )
        return json.dumps({"files": files})

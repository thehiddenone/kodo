"""``guided_dev_status`` tool — scans tracked documents' status (Guided mode only)."""

from __future__ import annotations

import json
from pathlib import Path

from kodo.guided_state import scan_tracked_files
from kodo.workproducts import read_work_products

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
        # A file's findings live in the backlog of the work product it belongs
        # to, not under its own path, so a per-file status has to get there
        # through the membership log. That log is project-scoped, so it is read
        # once per root and indexed here rather than per file.
        members: dict[str, str] = {}
        for root in roots:
            for work_product in read_work_products(Path(root.path)):
                for member in work_product.paths:
                    members[member] = work_product.id

        files: list[dict[str, object]] = []
        for root in roots:
            for entry in scan_tracked_files(Path(root.path)):
                logical = f"{root.name}/{entry['path']}"
                # A file in no work product has never been reviewed this
                # session; an empty key reads as an empty backlog, which is
                # exactly right.
                files.append(
                    {
                        "path": logical,
                        "status": status_from_state(
                            entry, ctx.findings_dir, members.get(logical, "")
                        ),
                        "work_product": members.get(logical, ""),
                        "last_event": entry["last_event"],
                    }
                )
        return json.dumps({"files": files})

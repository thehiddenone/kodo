"""``guided_dev_status`` tool — scans tracked documents' status (Guided mode only)."""

from __future__ import annotations

import json

from kodo.guided_state import scan_tracked_files

from ._tool import Tool

__all__ = ["GuidedDevStatusTool"]


class GuidedDevStatusTool(Tool):
    """Report every tracked document's current status."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        if ctx.mode != "guided":
            return json.dumps({"error": "guided_dev_status is only available in Guided mode."})
        if ctx.project_root is None:
            return json.dumps({"error": "No project is bound."})
        files = scan_tracked_files(ctx.project_root)
        return json.dumps({"files": files})

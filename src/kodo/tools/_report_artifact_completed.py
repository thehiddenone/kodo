"""``report_artifact_completed`` tool — promotes a gate-passed artifact."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["ReportArtifactCompletedTool"]

_log = logging.getLogger(__name__)


class ReportArtifactCompletedTool(Tool):
    """Mark an artifact completed via the injected completion callback.

    The callback (``complete_fn``) promotes the artifact (materialize + mirror
    commit + move out of staging) and flips its index entry to ``completed``.
    Unlike ``escalate_blocker``, completion does not force the run to stop.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        artifact_id = str(tool_input.get("artifact_id", ""))
        _log.info("report_artifact_completed from %s: id=%s", ctx.agent_name, artifact_id[:8])
        await ctx.complete_fn(artifact_id)
        return json.dumps({"status": "completed", "artifact_id": artifact_id})

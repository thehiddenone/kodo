"""``request_user_review_artifact`` tool — fires an artifact review gate."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["RequestUserReviewArtifactTool"]

_log = logging.getLogger(__name__)


class RequestUserReviewArtifactTool(Tool):
    """Present an artifact to the user for sign-off and return their verdict.

    In autonomous mode the user is away, so the gate auto-accepts.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        artifact_id = str(tool_input.get("artifact_id", ""))
        summary = str(tool_input.get("summary", "")) or "Please review this artifact."
        _log.info("request_user_review_artifact from %s: id=%s", ctx.agent_name, artifact_id[:8])

        if ctx.autonomous:
            return json.dumps({"action": "agree", "feedback": ""})

        gate_type = "review"
        try:
            arts = await ctx.workspace.read(artifact_id=artifact_id, include_content=False)
            if arts:
                gate_type = arts[0].type.value
        except Exception:  # pragma: no cover - label derivation is best-effort
            pass
        response = await ctx.gate.fire_approval(gate_type, artifact_id=artifact_id, summary=summary)
        return json.dumps({"action": response.action, "feedback": response.feedback})

"""``document_feedback`` tool — a critic's review verdict on one file."""

from __future__ import annotations

import json
import logging

from kodo.guided_state import append_feedback

from ._tool import Tool

__all__ = ["DocumentFeedbackTool"]

_log = logging.getLogger(__name__)


class DocumentFeedbackTool(Tool):
    """Record a critic's verdict on one file's evolution log."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        accept = bool(tool_input.get("accept", False))
        concerns_raw = tool_input.get("concerns", [])
        concerns = concerns_raw if isinstance(concerns_raw, list) else []
        summary = str(tool_input.get("summary", ""))

        if not accept and not concerns:
            return json.dumps({"error": "concerns must be non-empty when accept is false."})
        if ctx.project_root is None:
            return json.dumps(
                {"error": "No project is bound; document_feedback requires an active project."}
            )

        try:
            target = ctx.resolver.resolve(path)
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})

        try:
            append_feedback(
                target,
                ctx.project_root,
                reviewer=ctx.agent_name,
                accept=accept,
                concerns=concerns,
                summary=summary,
            )
        except ValueError as exc:
            _log.info("document_feedback from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        return json.dumps({"status": "recorded", "path": path})

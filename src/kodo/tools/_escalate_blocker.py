"""``escalate_blocker`` tool — hands control back to the orchestrator."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["EscalateBlockerTool"]

_log = logging.getLogger(__name__)


class EscalateBlockerTool(Tool):
    """Escalate a blocker, ending the agent's turn.

    Sets the context's ``stop_requested`` so the engine returns control to the
    orchestrator. In interactive mode the blocker is also surfaced to the
    present user; in autonomous mode the orchestrator adjudicates.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        reason = str(tool_input.get("reason", ""))
        summary = str(tool_input.get("summary", ""))
        _log.info("escalate_blocker from %s: reason=%s %s", ctx.agent_name, reason, summary[:80])

        ctx.stop_requested = True
        if ctx.session.effective_autonomous:
            return json.dumps({"status": "escalated", "reason": reason})
        response = await ctx.gate.fire_question(summary, "free_text")
        return json.dumps(
            {"status": "escalated", "reason": reason, "user_response": response.answer_text}
        )

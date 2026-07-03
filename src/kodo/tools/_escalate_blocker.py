"""``escalate_blocker`` tool — hands control back to the guide."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["EscalateBlockerTool"]

_log = logging.getLogger(__name__)


class EscalateBlockerTool(Tool):
    """Escalate a blocker, ending the agent's turn.

    Sets the context's ``stop_requested`` so the engine returns control to the
    guide. In interactive mode the blocker is also surfaced to the
    present user; in autonomous mode the guide adjudicates.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        reason = str(tool_input.get("reason", ""))
        summary = str(tool_input.get("summary", ""))
        _log.info("escalate_blocker from %s: reason=%s %s", ctx.agent_name, reason, summary[:80])

        ctx.stop_requested = True
        if ctx.session.effective_autonomous:
            return json.dumps({"status": "escalated", "reason": reason})
        # A blocker has no candidate answers to offer, so it rides the question
        # gate as a single free-text-only question (options=[] → the panel
        # renders just the free-text field).
        answers = await ctx.gate.fire_questions(
            [{"question": summary, "kind": "single_choice", "options": []}],
            ctx.current_tool_use_id,
        )
        user_response = str(answers[0].get("free_text") or "") if answers else ""
        return json.dumps(
            {"status": "escalated", "reason": reason, "user_response": user_response}
        )

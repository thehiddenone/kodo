"""``ask_user`` tool — surfaces a question to the present user.

Shared by every agent that declares ``ask_user`` (guide included).
The tool is withheld entirely in autonomous mode by ``subagents._registry``,
so this handler only ever runs when a user is present.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["AskUserTool"]

_log = logging.getLogger(__name__)


class AskUserTool(Tool):
    """Ask the user a free-text or choice question and return their answer."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        question = str(tool_input.get("question", ""))
        mode = str(tool_input.get("mode", "free_text"))
        choices_raw = tool_input.get("choices")
        choices: list[dict[str, str]] | None = None
        if isinstance(choices_raw, list):
            choices = [
                {"key": str(c.get("key", "")), "label": str(c.get("label", ""))}
                for c in choices_raw
                if isinstance(c, dict)
            ]

        _log.info("ask_user from %s: %r mode=%s", ctx.agent_name, question[:80], mode)
        response = await ctx.gate.fire_question(question, mode, choices)

        if mode == "choice":
            return json.dumps({"choice_key": response.choice_key})
        return json.dumps({"answer_text": response.answer_text})

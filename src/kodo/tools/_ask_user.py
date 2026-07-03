"""``ask_user`` tool — surfaces a batch of questions to the present user.

Shared by every agent that declares ``ask_user`` (guide included).
The tool is withheld entirely in autonomous mode by ``subagents._registry``,
so this handler only ever runs when a user is present.

The handler validates and normalizes the question batch, fires it through the
gate (one ``prompt.question`` request carrying every question plus this call's
``tool_use_id``, so the client can correlate the interactive panel with the
persisted feed entry), and blocks until the user confirms answers to all of
them.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["AskUserTool"]

_log = logging.getLogger(__name__)

_QUESTION_KINDS = ("single_choice", "multi_choice")


class AskUserTool(Tool):
    """Ask the user a batch of questions and return their confirmed answers."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        raw_questions = tool_input.get("questions")
        if not isinstance(raw_questions, list) or not raw_questions:
            return json.dumps({"error": "'questions' must be a non-empty array; retry."})

        questions: list[dict[str, object]] = []
        for i, raw in enumerate(raw_questions):
            if not isinstance(raw, dict):
                return json.dumps({"error": f"questions[{i}] is not an object; retry."})
            question = str(raw.get("question", "")).strip()
            kind = str(raw.get("kind", ""))
            options_raw = raw.get("options")
            options = (
                [str(o) for o in options_raw if str(o).strip()]
                if isinstance(options_raw, list)
                else []
            )
            if not question:
                return json.dumps({"error": f"questions[{i}].question is empty; retry."})
            if kind not in _QUESTION_KINDS:
                return json.dumps(
                    {
                        "error": (
                            f"questions[{i}].kind must be 'single_choice' or 'multi_choice'; retry."
                        )
                    }
                )
            if not options:
                return json.dumps(
                    {
                        "error": (
                            f"questions[{i}].options must list at least one candidate "
                            "answer (your best assumption first); retry."
                        )
                    }
                )
            questions.append({"question": question, "kind": kind, "options": options})

        _log.info(
            "ask_user from %s: %d question(s), first=%r",
            ctx.agent_name,
            len(questions),
            str(questions[0]["question"])[:80],
        )
        answers = await ctx.gate.fire_questions(questions, ctx.current_tool_use_id)
        return json.dumps({"answers": answers})

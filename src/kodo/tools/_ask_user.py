"""``ask_user`` tool — surfaces a batch of questions to the present user.

Shared by every agent that declares ``ask_user`` (guide included). Always
granted, in both interactive and autonomous sessions — the registry no longer
withholds it by mode (see ``ASK_USER.autonomous_mode`` in
``kodo.toolspecs``). Agent prompts are written to call this tool unconditionally
and never branch on mode themselves.

The handler validates and normalizes the question batch. When
``ctx.session.effective_autonomous`` is ``True`` there is no one to answer, so
it skips the gate entirely and synthesizes an answer per question instead of
blocking: a ``single_choice`` question resolves to its first option (the
agent's own stated best guess — see ``ASK_USER``'s ``options`` field, whose
first entry is always that); a ``multi_choice`` question comes back with no
``selected`` options and a fixed ``free_text`` notice telling the agent nobody
is present and it should decide for itself (see
``_AUTONOMOUS_MULTI_CHOICE_NOTICE``). Otherwise it fires the question batch
through the gate (one ``prompt.question`` request carrying every question plus
this call's ``tool_use_id``, so the client can correlate the interactive panel
with the persisted feed entry) and blocks until the user confirms answers to
all of them.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["AskUserTool"]

_log = logging.getLogger(__name__)

_QUESTION_KINDS = ("single_choice", "multi_choice")

# Returned as a multi_choice question's free_text when the session is
# autonomous (no one to answer). Distinct from a real user's free text so an
# agent reading it recognizes it as an instruction to decide for itself, not a
# genuine preference — see the "Asking the User Questions" preamble section.
_AUTONOMOUS_MULTI_CHOICE_NOTICE = (
    "User is away and cannot answer this question — choose whichever "
    "option(s) fit best in your own judgment, and proceed."
)


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

        if ctx.session.effective_autonomous:
            _log.info(
                "ask_user from %s: %d question(s) auto-answered (no user present), first=%r",
                ctx.agent_name,
                len(questions),
                str(questions[0]["question"])[:80],
            )
            answers = [self._synthesize_answer(q) for q in questions]
            return json.dumps({"answers": answers})

        _log.info(
            "ask_user from %s: %d question(s), first=%r",
            ctx.agent_name,
            len(questions),
            str(questions[0]["question"])[:80],
        )
        answers = await ctx.gate.fire_questions(questions, ctx.current_tool_use_id)
        return json.dumps({"answers": answers})

    @staticmethod
    def _synthesize_answer(question: dict[str, object]) -> dict[str, object]:
        """Answer *question* without a user, per its ``kind``.

        ``single_choice`` takes the first option — the agent's own stated best
        guess, by the "best assumption first" discipline every question is
        built under. ``multi_choice`` cannot be defaulted the same way (several
        options may legitimately apply), so it comes back empty-selected with
        ``_AUTONOMOUS_MULTI_CHOICE_NOTICE`` in ``free_text`` instead, telling
        the agent to decide for itself.
        """
        if question["kind"] == "single_choice":
            options = question["options"]
            assert isinstance(options, list) and options
            return {"selected": [options[0]], "free_text": None}
        return {"selected": [], "free_text": _AUTONOMOUS_MULTI_CHOICE_NOTICE}

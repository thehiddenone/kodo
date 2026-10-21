"""Agent Review → Agent Builder handoff (doc/AGENT_BUILDER.md §5.3).

Agent Review (``kodo_agent_review``) runs locked, in autonomous mode, and ends
with one ``report_agent_review`` call. :class:`AgentReviewHandoff` takes it from
there:

1. **Show.** :meth:`AgentReviewHandoff.report` renders the review as Markdown
   callouts and posts it as one persisted entry (``agent_review.report`` +
   an ``agent_review`` marker).
2. **Ask.** When the review holds concerns, :meth:`AgentReviewHandoff.turn_ended`
   asks the user — an engine-fired ``prompt.question``, which reaches the user
   even though the session is autonomous — whether to open Agent Builder.
3. **Hand off.** On a yes, the answer is persisted (an ``agent_handoff``
   marker) and ``agent.handoff`` tells the client to open a new interactive
   session on Agent Builder whose first prompt is the review
   (:func:`handoff_prompt`). Free text typed into the panel counts as a yes
   and travels with it as the user's directions.

The question is asked once per review: a resumed engine asks it again only
while no ``agent_handoff`` marker answers it (:meth:`AgentReviewHandoff.resume`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from kodo.state import TransientStore
from kodo.tools import AgentReview, AgentReviewConcern, GateLike

from ._events import EngineEmitters

__all__ = ["AGENT_BUILDER_AGENT", "HANDOFF_PROMPT_HEADING", "AgentReviewHandoff", "handoff_prompt"]

_log = logging.getLogger(__name__)

#: The interactive agent a review hands its concerns to.
AGENT_BUILDER_AGENT = "kodo_agent_builder"

#: How a handoff prompt starts — Agent Builder's prompt keys its review path on it.
HANDOFF_PROMPT_HEADING = "# Agent Review: "

_YES = "Yes, open Agent Builder"
_NO = "No"

# A callout tag in prose would open or close a callout of its own in the
# client's renderer, which skips tags only inside code.
_CALLOUT_TAG_RE = re.compile(r"</?kodo(?:_[a-z]+)?>")
_CODE_SPAN_RE = re.compile(r"(`+)[\s\S]*?\1")


class AgentReviewHandoff:
    """Shows one session's agent review and offers Agent Builder on its concerns.

    One per engine. Only Agent Review holds ``report_agent_review``, so in
    every other session this stays idle.
    """

    __emitters: EngineEmitters
    __gate: GateLike
    __transient: TransientStore
    __review: AgentReview | None
    __question_id: str
    __answered: bool
    __task: asyncio.Task[None] | None

    def __init__(
        self, *, emitters: EngineEmitters, gate: GateLike, transient: TransientStore
    ) -> None:
        """Bind the handoff to the engine's collaborators.

        Args:
            emitters (EngineEmitters): Posts the review and the handoff, and
                persists their markers.
            gate (GateLike): Fires the Agent Builder question.
            transient (TransientStore): The session log a resumed engine reads
                the review back from.
        """
        self.__emitters = emitters
        self.__gate = gate
        self.__transient = transient
        self.__review = None
        self.__question_id = ""
        self.__answered = False
        self.__task = None

    async def report(self, review: AgentReview) -> str | None:
        """Post *review* in the session; remember its concerns for the question.

        Args:
            review (AgentReview): The verdict ``report_agent_review`` checked.

        Returns:
            str | None: ``None`` when posted, else why not — a session reports
            one review.
        """
        if self.__review is not None:
            return "This session already reported its review; end your turn."
        self.__review = review
        self.__question_id = f"agent-handoff-{uuid.uuid4().hex}"
        self.__answered = review.accept
        record: dict[str, object] = {
            "agent": review.name,
            "accept": review.accept,
            "concerns": [_concern_record(c) for c in review.concerns],
            "summary": review.summary,
            "question_id": self.__question_id,
        }
        await self.__emitters.emit_agent_review(_render_review(review), record)
        _log.info(
            "Agent review reported: agent=%s accept=%s concerns=%d",
            review.name,
            review.accept,
            len(review.concerns),
        )
        return None

    def turn_ended(self) -> None:
        """Ask the Agent Builder question, if a review with concerns awaits it.

        Called once each top-level turn is over — finished, resumed or
        stopped. Fire-and-forget: the question waits on the user, and nothing
        else waits on the question.
        """
        if self.__review is None or self.__answered:
            return
        if self.__task is not None and not self.__task.done():
            return
        self.__task = asyncio.create_task(self.__ask(self.__review), name="kodo-agent-handoff")

    def resume(self) -> None:
        """Read this session's review back from its log, for a resumed engine.

        The last ``agent_review`` marker is the session's review; it is still
        waiting on the user when it holds concerns and no ``agent_handoff``
        marker answers its question. The caller asks it again through
        :meth:`turn_ended` once no turn is left to resume.
        """
        lines = self.__transient.read_session_lines()
        marker = next((ln for ln in reversed(lines) if ln.get("type") == "agent_review"), None)
        if marker is None:
            return
        review = _review_from_record(marker)
        if review is None:
            return
        self.__review = review
        self.__question_id = str(marker.get("question_id", ""))
        self.__answered = review.accept or any(
            ln.get("type") == "agent_handoff" and ln.get("question_id") == self.__question_id
            for ln in lines
        )

    async def __ask(self, review: AgentReview) -> None:
        count = len(review.concerns)
        questions: list[dict[str, object]] = [
            {
                "question": (
                    f"Open Agent Builder to fix the {_plural(count, 'concern')} in "
                    f"`{review.name}`? Anything you type goes to Agent Builder as directions."
                ),
                "kind": "single_choice",
                "options": [_YES, _NO],
            }
        ]
        try:
            answers = await self.__gate.fire_questions(questions, self.__question_id)
        except asyncio.CancelledError:
            return
        except Exception:
            _log.exception("Agent Builder question failed for agent=%s", review.name)
            return
        answer = answers[0] if answers else {}
        selected = answer.get("selected")
        free_text = answer.get("free_text")
        directions = free_text.strip() if isinstance(free_text, str) else ""
        opened = not (isinstance(selected, list) and _NO in selected)
        self.__answered = True
        self.__emitters.record_agent_handoff(
            {
                "question_id": self.__question_id,
                "questions": questions,
                "answers": answers,
                "open": opened,
            }
        )
        _log.info("Agent Builder question answered: agent=%s open=%s", review.name, opened)
        if opened:
            await self.__emitters.emit_agent_handoff(
                AGENT_BUILDER_AGENT, handoff_prompt(review, directions)
            )


def handoff_prompt(review: AgentReview, directions: str) -> str:
    """The first prompt of the Agent Builder session a review hands off to.

    Starts with :data:`HANDOFF_PROMPT_HEADING` and the agent's name, which is
    what Agent Builder's review path reads. Quotes sit in fenced blocks, so a
    callout tag inside one renders as text.

    Args:
        review (AgentReview): The review handed off.
        directions (str): What the user typed into the question panel, or ``""``.

    Returns:
        str: Markdown — the agent, every concern verbatim, the summary, and the
        user's directions when there are any.
    """
    count = len(review.concerns)
    parts = [
        f"{HANDOFF_PROMPT_HEADING}{review.name}",
        (
            f"Agent Review found {_plural(count, 'concern')} in the user agent "
            f"{_code(review.name)}. Fix every one. This review counts as the agent's "
            "first review."
        ),
        f"Summary: {_prose(review.summary)}",
    ]
    for index, concern in enumerate(review.concerns, start=1):
        parts.append(f"## Concern {index}")
        parts.append(f"Rule: {_code(concern.rule)}")
        parts.append(f"Location: {_code(concern.location) if concern.location else '(none)'}")
        parts.append(f"Quote:\n\n{_fenced(concern.quote)}" if concern.quote else "Quote: (none)")
        parts.append(f"Fix: {_prose(concern.fix)}")
    if directions:
        parts.append("## Directions from the user")
        parts.append(_prose(directions))
    return "\n\n".join(parts) + "\n"


def _render_review(review: AgentReview) -> str:
    """*review* as the callouts the session shows: one per concern, then the summary."""
    blocks: list[str] = []
    for concern in review.concerns:
        lines = [f"**{_prose(concern.rule)}**"]
        if concern.location:
            lines.append(_code(concern.location))
        if concern.quote:
            lines.append(_fenced(concern.quote))
        lines.append(f"Fix: {_prose(concern.fix)}")
        blocks.append("<kodo_warn>" + "\n\n".join(lines) + "</kodo_warn>")
    summary = _prose(review.summary)
    if review.accept:
        blocks.append(f"<kodo>{_code(review.name)} passed the review. {summary}</kodo>")
    else:
        count = _plural(len(review.concerns), "concern")
        blocks.append(f"<kodo_info>{_code(review.name)}: {count}. {summary}</kodo_info>")
    return "\n\n".join(blocks)


def _concern_record(concern: AgentReviewConcern) -> dict[str, object]:
    return {
        "rule": concern.rule,
        "quote": concern.quote,
        "location": concern.location,
        "fix": concern.fix,
    }


def _review_from_record(marker: dict[str, object]) -> AgentReview | None:
    """The review an ``agent_review`` marker records, or ``None`` for a malformed one."""
    raw = marker.get("concerns")
    if not isinstance(raw, list):
        return None
    concerns = tuple(
        AgentReviewConcern(
            rule=str(c.get("rule", "")),
            quote=str(c.get("quote", "")),
            location=str(c.get("location", "")),
            fix=str(c.get("fix", "")),
        )
        for c in raw
        if isinstance(c, dict)
    )
    return AgentReview(
        name=str(marker.get("agent", "")),
        accept=marker.get("accept") is True,
        concerns=concerns,
        summary=str(marker.get("summary", "")),
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _code(text: str) -> str:
    """*text* as one inline code span, fenced longer than any backtick run in it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _fenced(text: str) -> str:
    """*text* as a fenced code block, fenced longer than any backtick run in it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _prose(text: str) -> str:
    """*text* with every callout tag outside a code span turned into a code span."""
    out: list[str] = []
    last = 0
    for span in _CODE_SPAN_RE.finditer(text):
        out.append(_CALLOUT_TAG_RE.sub(lambda m: f"`{m.group(0)}`", text[last : span.start()]))
        out.append(span.group(0))
        last = span.end()
    out.append(_CALLOUT_TAG_RE.sub(lambda m: f"`{m.group(0)}`", text[last:]))
    return "".join(out)

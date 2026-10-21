"""Behavioral tests for the Agent Review → Agent Builder handoff (doc/AGENT_BUILDER.md §5.3).

Drives :class:`AgentReviewHandoff` against a real :class:`TransientStore` and
:class:`EngineEmitters`, with only the client faked: a sink that records
envelopes and a gate whose question answers are scripted. Asserts what the user
sees (the posted review, the question), what is persisted (the markers a reload
or a resume reads), and what the client is told to open.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kodo.common import Envelope
from kodo.runtime import SessionState
from kodo.runtime._engine import (
    AGENT_BUILDER_AGENT,
    HANDOFF_PROMPT_HEADING,
    AgentReviewHandoff,
    EngineEmitters,
    handoff_prompt,
)
from kodo.state import TransientStore
from kodo.tools import AgentReview, AgentReviewConcern
from kodo.transport import EVT_AGENT_HANDOFF, EVT_AGENT_REVIEW


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[Envelope] = []

    async def send(self, env: Envelope) -> None:
        self.sent.append(env)

    def events(self, event_type: str) -> list[dict[str, object]]:
        return [
            env.payload
            for env in self.sent
            if env.kind == "event" and env.payload.get("type") == event_type
        ]


class _FakeGate:
    """Answers every question batch with *answer*, after *release* is set."""

    def __init__(self, answer: dict[str, object]) -> None:
        self.answer = answer
        self.release = asyncio.Event()
        self.release.set()
        self.calls: list[tuple[list[dict[str, object]], str]] = []

    async def fire_questions(
        self, questions: list[dict[str, object]], tool_call_id: str = ""
    ) -> list[dict[str, object]]:
        self.calls.append((questions, tool_call_id))
        await self.release.wait()
        return [self.answer]


_YES = {"selected": ["Yes, open Agent Builder"], "free_text": None}
_NO = {"selected": ["No"], "free_text": None}

_CONCERNS = (
    AgentReviewConcern(
        rule="L4 purpose",
        quote="You are **Acme**: you help.",
        location="agent_acme_helper.md:12",
        fix="Name one purpose in the opening sentence.",
    ),
    AgentReviewConcern(
        rule="kodo-prompt-writing: Output contracts",
        quote="Write one <kodo_warn> per concern.</kodo_warn>",
        location="## Reporting",
        fix="Close with a <kodo> summary and say when it is written.",
    ),
)
_REJECTED = AgentReview(
    name="acme_helper", accept=False, concerns=_CONCERNS, summary="Rejected: 2 concerns."
)
_ACCEPTED = AgentReview(name="acme_helper", accept=True, concerns=(), summary="Accepted.")


def _make(
    tmp_path: Path, answer: dict[str, object] = _YES, *, resumed: bool = False
) -> tuple[AgentReviewHandoff, _FakeSink, _FakeGate, TransientStore]:
    transient = TransientStore(tmp_path)
    transient.attach_session("sess-1", resumed=resumed)
    sink = _FakeSink()
    gate = _FakeGate(answer)
    emitters = EngineEmitters(
        sink, SessionState(session_id="sess-1"), context_stats=lambda: {}, transient=transient
    )
    return (
        AgentReviewHandoff(emitters=emitters, gate=gate, transient=transient),
        sink,
        gate,
        transient,
    )


async def _settle() -> None:
    """Let the fire-and-forget question task run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


def _markers(transient: TransientStore, kind: str) -> list[dict[str, object]]:
    return [ln for ln in transient.read_session_lines() if ln.get("type") == kind]


async def test_report_posts_every_concern_and_the_summary(tmp_path: Path) -> None:
    handoff, sink, _gate, transient = _make(tmp_path)

    assert await handoff.report(_REJECTED) is None

    (event,) = sink.events(EVT_AGENT_REVIEW)
    text = str(event["text"])
    assert text.count("<kodo_warn>**") == 2
    assert "**L4 purpose**" in text
    assert "`agent_acme_helper.md:12`" in text
    assert "You are **Acme**: you help." in text
    assert "Fix: Name one purpose in the opening sentence." in text
    assert "<kodo_info>`acme_helper`: 2 concerns. Rejected: 2 concerns.</kodo_info>" in text
    (marker,) = _markers(transient, "agent_review")
    assert marker["text"] == text
    assert marker["agent"] == "acme_helper"
    assert marker["accept"] is False


async def test_callout_tags_in_a_concern_do_not_open_callouts_of_their_own(
    tmp_path: Path,
) -> None:
    handoff, sink, _gate, _transient = _make(tmp_path)

    await handoff.report(_REJECTED)

    text = str(sink.events(EVT_AGENT_REVIEW)[0]["text"])
    # The quote sits in a fenced block and a tag in prose becomes inline code,
    # so the only bare tags left are the callouts the engine wrote itself.
    assert "```text\nWrite one <kodo_warn> per concern.</kodo_warn>\n```" in text
    assert "Close with a `<kodo>` summary" in text
    bare = text.replace("```text\nWrite one <kodo_warn> per concern.</kodo_warn>\n```", "")
    bare = bare.replace("`<kodo>`", "")
    assert bare.count("<kodo_warn>") == bare.count("</kodo_warn>") == 2


async def test_a_passed_review_says_so_and_asks_nothing(tmp_path: Path) -> None:
    handoff, sink, gate, _transient = _make(tmp_path)

    await handoff.report(_ACCEPTED)
    handoff.turn_ended()
    await _settle()

    text = str(sink.events(EVT_AGENT_REVIEW)[0]["text"])
    assert text == "<kodo>`acme_helper` passed the review. Accepted.</kodo>"
    assert gate.calls == []


async def test_a_session_reports_one_review(tmp_path: Path) -> None:
    handoff, sink, _gate, _transient = _make(tmp_path)

    await handoff.report(_REJECTED)
    refusal = await handoff.report(_ACCEPTED)

    assert refusal is not None
    assert len(sink.events(EVT_AGENT_REVIEW)) == 1


async def test_nothing_is_asked_before_the_turn_ends(tmp_path: Path) -> None:
    handoff, _sink, gate, _transient = _make(tmp_path)

    await handoff.report(_REJECTED)
    await _settle()

    assert gate.calls == []


async def test_yes_opens_agent_builder_on_the_review(tmp_path: Path) -> None:
    handoff, sink, gate, transient = _make(tmp_path, _YES)

    await handoff.report(_REJECTED)
    handoff.turn_ended()
    await _settle()

    ((questions, question_id),) = gate.calls
    assert questions[0]["kind"] == "single_choice"
    assert questions[0]["options"] == ["Yes, open Agent Builder", "No"]
    assert "acme_helper" in str(questions[0]["question"])
    (handoff_event,) = sink.events(EVT_AGENT_HANDOFF)
    assert handoff_event["name"] == AGENT_BUILDER_AGENT
    assert handoff_event["prompt"] == handoff_prompt(_REJECTED, "")
    (answer,) = _markers(transient, "agent_handoff")
    assert answer["question_id"] == question_id
    assert answer["open"] is True
    assert answer["answers"] == [_YES]


async def test_no_opens_nothing(tmp_path: Path) -> None:
    handoff, sink, _gate, transient = _make(tmp_path, _NO)

    await handoff.report(_REJECTED)
    handoff.turn_ended()
    await _settle()

    assert sink.events(EVT_AGENT_HANDOFF) == []
    (answer,) = _markers(transient, "agent_handoff")
    assert answer["open"] is False


@pytest.mark.parametrize(
    "answer",
    [
        {"selected": [], "free_text": "Keep the persona."},
        {"selected": ["Yes, open Agent Builder"], "free_text": "Keep the persona."},
    ],
)
async def test_typed_text_opens_agent_builder_with_the_users_directions(
    tmp_path: Path, answer: dict[str, object]
) -> None:
    handoff, sink, _gate, _transient = _make(tmp_path, answer)

    await handoff.report(_REJECTED)
    handoff.turn_ended()
    await _settle()

    prompt = str(sink.events(EVT_AGENT_HANDOFF)[0]["prompt"])
    assert prompt.endswith("## Directions from the user\n\nKeep the persona.\n")


async def test_the_question_is_asked_once_while_it_waits(tmp_path: Path) -> None:
    handoff, _sink, gate, _transient = _make(tmp_path)
    gate.release.clear()

    await handoff.report(_REJECTED)
    handoff.turn_ended()
    await _settle()
    handoff.turn_ended()
    await _settle()
    gate.release.set()
    await _settle()
    handoff.turn_ended()
    await _settle()

    assert len(gate.calls) == 1


async def test_a_resumed_session_asks_an_unanswered_question_again(tmp_path: Path) -> None:
    first, _sink, gate, _transient = _make(tmp_path)
    gate.release.clear()
    await first.report(_REJECTED)
    first.turn_ended()
    await _settle()
    (_, question_id), *_rest = gate.calls

    resumed, sink, resumed_gate, _t = _make(tmp_path, _YES, resumed=True)
    resumed.resume()
    resumed.turn_ended()
    await _settle()

    assert [call_id for _q, call_id in resumed_gate.calls] == [question_id]
    assert sink.events(EVT_AGENT_HANDOFF)[0]["prompt"] == handoff_prompt(_REJECTED, "")
    assert await resumed.report(_ACCEPTED) is not None


async def test_a_resumed_session_never_asks_an_answered_question_again(tmp_path: Path) -> None:
    first, _sink, _gate, _transient = _make(tmp_path, _NO)
    await first.report(_REJECTED)
    first.turn_ended()
    await _settle()

    resumed, _s, resumed_gate, _t = _make(tmp_path, _YES, resumed=True)
    resumed.resume()
    resumed.turn_ended()
    await _settle()

    assert resumed_gate.calls == []


async def test_a_resumed_session_without_a_review_asks_nothing(tmp_path: Path) -> None:
    _make(tmp_path)
    resumed, _s, gate, _t = _make(tmp_path, resumed=True)
    resumed.resume()
    resumed.turn_ended()
    await _settle()

    assert gate.calls == []


def test_handoff_prompt_carries_the_review_verbatim() -> None:
    prompt = handoff_prompt(_REJECTED, "")

    assert prompt.startswith(f"{HANDOFF_PROMPT_HEADING}acme_helper\n")
    assert "This review counts as the agent's first review." in prompt
    assert "## Concern 1" in prompt and "## Concern 2" in prompt
    assert "Rule: `L4 purpose`" in prompt
    assert "Location: `agent_acme_helper.md:12`" in prompt
    assert "```text\nYou are **Acme**: you help.\n```" in prompt
    assert "Fix: Name one purpose in the opening sentence." in prompt
    assert "Directions from the user" not in prompt


def test_handoff_prompt_fences_a_quote_longer_than_its_backticks() -> None:
    concern = AgentReviewConcern(
        rule="L7", quote="```\ncode\n```", location="x.md:1", fix="Drop it."
    )
    review = AgentReview(name="acme_x", accept=False, concerns=(concern,), summary="1 concern.")

    assert "````text\n```\ncode\n```\n````" in handoff_prompt(review, "")

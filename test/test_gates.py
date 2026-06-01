"""Behavior tests for kodo.runtime._gates.

Tests verify the kind=request / kind=response correlation model introduced in
WS_PROTOCOL.md §6.  Gates emit kind=request frames and block on a Future that
is resolved when AppState receives a matching kind=response.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.common import Envelope
from kodo.runtime._gates import ApprovalResponse, GateOrchestrator, QuestionResponse
from kodo.transport import SREQ_PROMPT_APPROVAL, SREQ_PROMPT_QUESTION

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state() -> MagicMock:
    """Mock AppState that records register_response_future calls."""
    state = MagicMock()
    state.send = AsyncMock()
    captured: dict[str, asyncio.Future[dict[str, object]]] = {}

    def _capture(request_id: str, future: asyncio.Future[dict[str, object]]) -> None:
        captured[request_id] = future

    state.register_response_future = _capture
    state._captured = captured
    return state


def _get_sent_payloads(state: MagicMock) -> list[dict[str, object]]:
    return [call.args[0].payload for call in state.send.call_args_list]


def _get_sent_envelopes(state: MagicMock) -> list[Envelope]:
    return [call.args[0] for call in state.send.call_args_list]


# ---------------------------------------------------------------------------
# ApprovalResponse
# ---------------------------------------------------------------------------


def test_approval_response_fields() -> None:
    r = ApprovalResponse(action="agree", feedback="")
    assert r.action == "agree"
    assert r.feedback == ""


def test_approval_response_with_feedback() -> None:
    r = ApprovalResponse(action="feedback", feedback="Please elaborate.")
    assert r.action == "feedback"
    assert r.feedback == "Please elaborate."


# ---------------------------------------------------------------------------
# QuestionResponse
# ---------------------------------------------------------------------------


def test_question_response_fields() -> None:
    r = QuestionResponse(answer_text="hello", choice_key="")
    assert r.answer_text == "hello"
    assert r.choice_key == ""


# ---------------------------------------------------------------------------
# fire_approval emits kind=request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_approval_sends_kind_request() -> None:
    """
    When fire_approval is called,
    then a kind=request frame with type=prompt.approval is emitted.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _fire_and_resolve() -> ApprovalResponse:
        task = asyncio.create_task(gate.fire_approval("narrative", summary="Ready"))
        await asyncio.sleep(0)  # let fire_approval register the future and send
        assert state._captured
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"action": "agree"})
        return await task

    response = await _fire_and_resolve()

    envelopes = _get_sent_envelopes(state)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.kind == "request"
    assert env.payload["type"] == SREQ_PROMPT_APPROVAL
    assert env.payload["gate_type"] == "narrative"
    assert response.action == "agree"


@pytest.mark.asyncio
async def test_fire_approval_agree_returns_empty_feedback() -> None:
    """
    When the user agrees with no feedback text,
    then ApprovalResponse.feedback is empty.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _run() -> ApprovalResponse:
        task = asyncio.create_task(gate.fire_approval("requirements", summary="s"))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"action": "agree"})
        return await task

    response = await _run()
    assert response.action == "agree"
    assert response.feedback == ""


@pytest.mark.asyncio
async def test_fire_approval_feedback_carries_text() -> None:
    """
    When the user provides feedback,
    then ApprovalResponse.feedback contains the text.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _run() -> ApprovalResponse:
        task = asyncio.create_task(gate.fire_approval("design", summary="s"))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result(
            {"action": "feedback", "feedback_text": "Add error handling."}
        )
        return await task

    response = await _run()
    assert response.action == "feedback"
    assert "error handling" in response.feedback


@pytest.mark.asyncio
async def test_fire_approval_request_id_matches_registered_future() -> None:
    """
    The request envelope id must match the key registered in register_response_future.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _run() -> None:
        task = asyncio.create_task(gate.fire_approval("plan", summary="s"))
        await asyncio.sleep(0)
        # The envelope id and the registered future key must be the same
        envelopes = _get_sent_envelopes(state)
        assert len(envelopes) == 1
        env = envelopes[0]
        assert env.id in state._captured, "envelope id must be in registered futures"
        state._captured[env.id].set_result({"action": "agree"})
        await task

    await _run()


# ---------------------------------------------------------------------------
# fire_question emits kind=request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_question_free_text_sends_kind_request() -> None:
    """
    When fire_question is called with mode='free_text',
    then a kind=request frame with type=prompt.question is emitted.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _run() -> QuestionResponse:
        task = asyncio.create_task(gate.fire_question("What should we build?", "free_text"))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"answer_text": "A trading bot"})
        return await task

    response = await _run()

    env = _get_sent_envelopes(state)[0]
    assert env.kind == "request"
    assert env.payload["type"] == SREQ_PROMPT_QUESTION
    assert env.payload["mode"] == "free_text"
    assert response.answer_text == "A trading bot"


@pytest.mark.asyncio
async def test_fire_question_choice_mode_returns_choice_key() -> None:
    """
    When fire_question is called with mode='choice' and the user picks a key,
    then QuestionResponse.choice_key carries the selection.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)
    choices = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]

    async def _run() -> QuestionResponse:
        task = asyncio.create_task(gate.fire_question("Ready?", "choice", choices=choices))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"choice_key": "yes"})
        return await task

    response = await _run()
    assert response.choice_key == "yes"
    assert response.answer_text == ""


@pytest.mark.asyncio
async def test_fire_question_choices_included_in_payload() -> None:
    """
    When choices are passed to fire_question,
    then the emitted payload includes the choices list.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state)
    choices = [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]

    async def _run() -> None:
        task = asyncio.create_task(gate.fire_question("Pick one", "choice", choices))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"choice_key": "a"})
        await task

    await _run()
    payload = _get_sent_payloads(state)[0]
    assert payload["choices"] == choices


# ---------------------------------------------------------------------------
# fire() alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_alias_behaves_like_fire_approval() -> None:
    """gate.fire() is an alias for gate.fire_approval()."""
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _run() -> ApprovalResponse:
        task = asyncio.create_task(gate.fire("implementation", summary="s"))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"action": "agree"})
        return await task

    response = await _run()
    assert response.action == "agree"
    env = _get_sent_envelopes(state)[0]
    assert env.payload["type"] == SREQ_PROMPT_APPROVAL

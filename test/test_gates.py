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
from kodo.runtime import ApprovalResponse, GateOrchestrator
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


def _make_transient() -> MagicMock:
    """Mock TransientStore that no-ops pending-prompt persistence."""
    transient = MagicMock()
    transient.update = MagicMock()
    return transient


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
# fire_approval emits kind=request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_approval_sends_kind_request() -> None:
    """
    When fire_approval is called,
    then a kind=request frame with type=prompt.approval is emitted.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state, _make_transient())

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
    gate = GateOrchestrator(state, _make_transient())

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
    gate = GateOrchestrator(state, _make_transient())

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
    gate = GateOrchestrator(state, _make_transient())

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
# fire_questions emits kind=request
# ---------------------------------------------------------------------------

_QUESTIONS: list[dict[str, object]] = [
    {
        "question": "Which DB should the service use?",
        "kind": "single_choice",
        "options": ["PostgreSQL", "SQLite"],
    },
    {
        "question": "Which features are in scope?",
        "kind": "multi_choice",
        "options": ["Auth", "Billing"],
    },
]


@pytest.mark.asyncio
async def test_fire_questions_sends_kind_request_with_batch() -> None:
    """
    When fire_questions is called,
    then one kind=request frame with type=prompt.question carries the whole
    batch plus the calling tool_use id.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state, _make_transient())

    async def _run() -> list[dict[str, object]]:
        task = asyncio.create_task(gate.fire_questions(_QUESTIONS, "tc-1"))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result(
            {
                "answers": [
                    {"selected": ["PostgreSQL"], "free_text": None},
                    {"selected": ["Auth"], "free_text": "also CSV export"},
                ]
            }
        )
        return await task

    answers = await _run()

    env = _get_sent_envelopes(state)[0]
    assert env.kind == "request"
    assert env.payload["type"] == SREQ_PROMPT_QUESTION
    assert env.payload["tool_call_id"] == "tc-1"
    assert env.payload["questions"] == _QUESTIONS
    assert answers == [
        {"selected": ["PostgreSQL"], "free_text": None},
        {"selected": ["Auth"], "free_text": "also CSV export"},
    ]


@pytest.mark.asyncio
async def test_fire_questions_normalizes_malformed_answers() -> None:
    """
    When the client response is missing or malformed,
    then every question still gets a well-formed empty answer entry.
    """
    state = _make_app_state()
    gate = GateOrchestrator(state, _make_transient())

    async def _run() -> list[dict[str, object]]:
        task = asyncio.create_task(gate.fire_questions(_QUESTIONS))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"answers": [{"selected": "not-a-list"}]})
        return await task

    answers = await _run()
    assert answers == [
        {"selected": [], "free_text": None},
        {"selected": [], "free_text": None},
    ]


@pytest.mark.asyncio
async def test_fire_questions_does_not_persist_pending_prompt() -> None:
    """
    fire_questions never records a pending_prompt: crash-resume re-drives the
    batch from the flushed tool_use instead of a persisted prompt record.
    """
    state = _make_app_state()
    transient = _make_transient()
    gate = GateOrchestrator(state, transient)

    async def _run() -> None:
        task = asyncio.create_task(gate.fire_questions(_QUESTIONS, "tc-2"))
        await asyncio.sleep(0)
        req_id = next(iter(state._captured))
        state._captured[req_id].set_result({"answers": []})
        await task

    await _run()
    transient.update.assert_not_called()


# ---------------------------------------------------------------------------
# fire() alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_alias_behaves_like_fire_approval() -> None:
    """gate.fire() is an alias for gate.fire_approval()."""
    state = _make_app_state()
    gate = GateOrchestrator(state, _make_transient())

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

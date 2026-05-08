"""Behavior tests for kodo.workflow._gates."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.workflow._gates import ApprovalResponse, GateOrchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state() -> MagicMock:
    state = MagicMock()
    state.send = AsyncMock()
    return state


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
# GateOrchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_fire_and_agree() -> None:
    state = _make_app_state()
    gate = GateOrchestrator(state)

    async def _resolve_later() -> None:
        await asyncio.sleep(0)
        gate.resolve("dummy", "agree", "")

    task = asyncio.create_task(_resolve_later())

    # Intercept the gate_id from the emitted event
    gate_id: str | None = None

    async def capture_send(env: object) -> None:
        from kodo.transport._envelope import Envelope

        if isinstance(env, Envelope) and env.payload.get("gate_type") == "test_gate":
            nonlocal gate_id
            gate_id = str(env.payload.get("gate_id", ""))

    state.send = capture_send

    async def _fire_and_resolve() -> ApprovalResponse:
        # Need the gate_id, so start fire, let asyncio run once, then resolve
        fire_task = asyncio.create_task(gate.fire("test_gate", summary="test summary"))
        await asyncio.sleep(0)  # let fire() emit the event
        if gate_id is not None:
            gate.resolve(gate_id, "agree", "")
        return await fire_task

    response = await _fire_and_resolve()
    assert response.action == "agree"
    assert response.feedback == ""
    task.cancel()


@pytest.mark.asyncio
async def test_gate_fire_and_feedback() -> None:
    state = _make_app_state()
    gate = GateOrchestrator(state)
    gate_id: str | None = None

    async def capture_send(env: object) -> None:
        from kodo.transport._envelope import Envelope

        if isinstance(env, Envelope) and env.payload.get("gate_type") == "narrative":
            nonlocal gate_id
            gate_id = str(env.payload.get("gate_id", ""))

    state.send = capture_send

    async def _fire_and_resolve() -> ApprovalResponse:
        fire_task = asyncio.create_task(gate.fire("narrative", summary="a narrative"))
        await asyncio.sleep(0)
        if gate_id is not None:
            gate.resolve(gate_id, "feedback", "Please add success criteria.")
        return await fire_task

    response = await _fire_and_resolve()
    assert response.action == "feedback"
    assert "success criteria" in response.feedback


@pytest.mark.asyncio
async def test_gate_fire_and_stop() -> None:
    state = _make_app_state()
    gate = GateOrchestrator(state)
    gate_id: str | None = None

    async def capture_send(env: object) -> None:
        from kodo.transport._envelope import Envelope

        if isinstance(env, Envelope) and env.payload.get("gate_type") == "narrative":
            nonlocal gate_id
            gate_id = str(env.payload.get("gate_id", ""))

    state.send = capture_send

    async def _fire_and_resolve() -> ApprovalResponse:
        fire_task = asyncio.create_task(gate.fire("narrative", summary="a narrative"))
        await asyncio.sleep(0)
        if gate_id is not None:
            gate.resolve(gate_id, "stop", "")
        return await fire_task

    response = await _fire_and_resolve()
    assert response.action == "stop"
    assert response.feedback == ""


@pytest.mark.asyncio
async def test_gate_resolve_unknown_id_returns_false() -> None:
    state = _make_app_state()
    gate = GateOrchestrator(state)
    result = gate.resolve("completely-unknown-id", "agree", "")
    assert result is False


@pytest.mark.asyncio
async def test_gate_fire_sends_approval_request_event() -> None:
    from kodo.transport._messages import EVT_APPROVAL_REQUEST

    state = _make_app_state()
    gate = GateOrchestrator(state)
    sent_payloads: list[dict[str, object]] = []

    async def capture_send(env: object) -> None:
        from kodo.transport._envelope import Envelope

        if isinstance(env, Envelope):
            sent_payloads.append(dict(env.payload))

    state.send = capture_send

    async def _fire_and_resolve() -> None:
        fire_task = asyncio.create_task(
            gate.fire(
                "narrative",
                artifact_path=Path("src/narrative.kd"),
                summary="My summary",
                component=None,
            )
        )
        await asyncio.sleep(0)
        gate_id = next(
            (str(p.get("gate_id", "")) for p in sent_payloads if p.get("gate_type") == "narrative"),
            None,
        )
        assert gate_id is not None
        gate.resolve(gate_id, "agree", "")
        await fire_task

    await _fire_and_resolve()

    assert any(p.get("type") == EVT_APPROVAL_REQUEST for p in sent_payloads)
    evt = next(p for p in sent_payloads if p.get("type") == EVT_APPROVAL_REQUEST)
    assert evt.get("gate_type") == "narrative"
    assert evt.get("summary") == "My summary"
    assert "src/narrative.kd" in str(evt.get("artifact_path", ""))

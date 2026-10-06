"""Behaviour tests for GateOrchestrator's less-travelled outcomes.

Complements ``test_gates.py`` with: the stuck-agent alarm and project-folder
picker requests (their payloads and response normalisation), and what each
persisted-marker gate (approval, permission, edit review) leaves behind on a
real :class:`~kodo.state.TransientStore` when its wait is cancelled versus
when it fails.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kodo.common import Envelope
from kodo.runtime import GateOrchestrator
from kodo.state import TransientStore
from kodo.transport import SREQ_PROMPT_CHOOSE_PROJECT_FOLDER, SREQ_PROMPT_STUCK_ALERT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeChannel:
    """In-memory ``ResponseChannel``: records sent envelopes and pending futures."""

    def __init__(self) -> None:
        self.sent: list[Envelope] = []
        self.futures: dict[str, asyncio.Future[dict[str, object]]] = {}

    async def send(self, env: Envelope) -> None:
        self.sent.append(env)

    def register_response_future(
        self, request_id: str, future: asyncio.Future[dict[str, object]]
    ) -> None:
        self.futures[request_id] = future

    def discard_response_future(self, request_id: str) -> None:
        self.futures.pop(request_id, None)

    def only_future(self) -> asyncio.Future[dict[str, object]]:
        assert len(self.futures) == 1
        return next(iter(self.futures.values()))


@pytest.fixture()
def transient(tmp_path: Path) -> TransientStore:
    store = TransientStore(tmp_path)
    store.attach_session("sess-1", resumed=False)
    return store


async def _respond[T](
    channel: _FakeChannel, task: asyncio.Task[T], payload: dict[str, object]
) -> T:
    await asyncio.sleep(0)
    channel.only_future().set_result(payload)
    return await task


async def _fail[T](channel: _FakeChannel, task: asyncio.Task[T]) -> None:
    await asyncio.sleep(0)
    channel.only_future().set_exception(RuntimeError("session torn down"))
    with pytest.raises(RuntimeError, match="session torn down"):
        await task


async def _cancel[T](task: asyncio.Task[T]) -> None:
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _permission_kwargs() -> dict[str, object]:
    return {
        "tool_call_id": "tu_perm",
        "tool_name": "run_command",
        "external_name": "Run Command",
        "risk": "high",
        "intent": "list files",
        "reason": "unknown executable",
        "params": [{"name": "command", "value": "ls"}],
    }


def _edit_review_kwargs() -> dict[str, object]:
    return {
        "tool_call_id": "tu_edit",
        "tool_name": "edit_file",
        "path": "src/foo.py",
        "mode": "modification",
        "old_content": "a\n",
        "new_content": "b\n",
    }


# ---------------------------------------------------------------------------
# fire_stuck_alert
# ---------------------------------------------------------------------------


async def test_stuck_alert_sends_request_and_returns_unstick(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(
        gate.fire_stuck_alert(agent_name="coder", display_name="Coder", reasons=["Looping"])
    )
    response = await _respond(channel, task, {"action": "unstick"})

    (env,) = channel.sent
    assert env.kind == "request"
    assert env.payload == {
        "type": SREQ_PROMPT_STUCK_ALERT,
        "agent_name": "coder",
        "display_name": "Coder",
        "reasons": ["Looping"],
    }
    assert response.action == "unstick"


@pytest.mark.parametrize("payload", [{}, {"action": "explode"}, {"action": "dismiss"}])
async def test_stuck_alert_missing_or_unknown_action_is_dismiss(
    transient: TransientStore, payload: dict[str, object]
) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(
        gate.fire_stuck_alert(agent_name="guide", display_name="Guide", reasons=[])
    )
    response = await _respond(channel, task, payload)

    assert response.action == "dismiss"


async def test_stuck_alert_persists_nothing(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(
        gate.fire_stuck_alert(agent_name="guide", display_name="Guide", reasons=[])
    )
    await asyncio.sleep(0)
    assert transient.pending_prompt is None
    assert transient.pending_security_alert is None
    assert transient.pending_edit_review is None
    await _respond(channel, task, {"action": "dismiss"})


# ---------------------------------------------------------------------------
# fire_choose_project_folder
# ---------------------------------------------------------------------------


async def test_choose_project_folder_returns_picked_path(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_choose_project_folder())
    response = await _respond(channel, task, {"path": "/home/me/projects"})

    (env,) = channel.sent
    assert env.kind == "request"
    assert env.payload == {"type": SREQ_PROMPT_CHOOSE_PROJECT_FOLDER}
    assert response.path == "/home/me/projects"
    assert response.error is None


async def test_choose_project_folder_reports_cancellation(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_choose_project_folder())
    response = await _respond(channel, task, {"error": "cancelled", "path": "/ignored"})

    assert response.path == ""
    assert response.error == "cancelled"


async def test_choose_project_folder_without_path_is_empty(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_choose_project_folder())
    response = await _respond(channel, task, {})

    assert response.path == ""
    assert response.error is None


# ---------------------------------------------------------------------------
# fire_approval: pending_prompt lifecycle
# ---------------------------------------------------------------------------


async def test_approval_persists_pending_prompt_while_waiting(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(
        gate.fire_approval("document_review", artifact_id="wp1", summary="s", paths=["a.md"])
    )
    await asyncio.sleep(0)

    assert transient.pending_prompt == {
        "kind": "approval",
        "gate_type": "document_review",
        "artifact_id": "wp1",
        "summary": "s",
        "paths": ["a.md"],
        "findings": [],
    }
    await _respond(channel, task, {"action": "agree"})
    assert transient.pending_prompt is None


async def test_approval_parses_artifact_path_and_resolved_findings(
    transient: TransientStore,
) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_approval("document_review", summary="s"))
    response = await _respond(
        channel,
        task,
        {
            "action": "feedback",
            "feedback_text": "fix it",
            "artifact_path": "a.md",
            "resolved_finding_ids": ["f1", "", 3, "f2"],
        },
    )

    assert response.artifact_path == "a.md"
    assert response.resolved_finding_ids == ("f1", "f2")


async def test_approval_cancellation_keeps_pending_prompt(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_approval("narrative", summary="s"))
    await _cancel(task)

    pending = transient.pending_prompt
    assert pending is not None
    assert pending["gate_type"] == "narrative"


async def test_approval_failure_clears_pending_prompt(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_approval("narrative", summary="s"))
    await _fail(channel, task)

    assert transient.pending_prompt is None


# ---------------------------------------------------------------------------
# fire_permission / fire_edit_review: failures clear the marker
# ---------------------------------------------------------------------------


async def test_permission_failure_clears_pending_security_alert(
    transient: TransientStore,
) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_permission(**_permission_kwargs()))
    await asyncio.sleep(0)
    assert transient.pending_security_alert == "tu_perm"
    await _fail(channel, task)

    assert transient.pending_security_alert is None


async def test_edit_review_failure_clears_pending_edit_review(transient: TransientStore) -> None:
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, transient)

    task = asyncio.create_task(gate.fire_edit_review(**_edit_review_kwargs()))
    await asyncio.sleep(0)
    assert transient.pending_edit_review == "tu_edit"
    await _fail(channel, task)

    assert transient.pending_edit_review is None


async def test_edit_review_cancellation_keeps_marker_on_disk(tmp_path: Path) -> None:
    store = TransientStore(tmp_path)
    store.attach_session("sess-1", resumed=False)
    channel = _FakeChannel()
    gate = GateOrchestrator(channel, store)

    task = asyncio.create_task(gate.fire_edit_review(**_edit_review_kwargs()))
    await _cancel(task)

    resumed = TransientStore(tmp_path)
    resumed.attach_session("sess-1", resumed=True)
    assert resumed.pending_edit_review == "tu_edit"

"""Behaviour tests for EngineEmitters' marker-persisting emitters.

Each "push + persist" emitter must (a) send its live event on the sink and
(b) durably append a matching marker to whichever log is active — the main
``session.jsonl`` normally, or the active subsession's own log while a
sub-agent is running — so the history projector can replay it on reload.
The nudge emitter is the deliberate exception: live push only.

``EngineEmitters`` is an internal collaborator of :class:`kodo.runtime.WorkflowEngine`
with no public re-export, so — like ``test_engine_shared_events.py`` — it is
constructed directly against a real :class:`~kodo.state.TransientStore` and a
recording sink.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from kodo.common import Envelope
from kodo.runtime import SessionState
from kodo.runtime._engine._events import EngineEmitters
from kodo.state import TransientStore
from kodo.transport import (
    EVT_AGENT_CYCLIC_THINKING_CRITICAL,
    EVT_AGENT_STUCK_CRITICAL,
    EVT_AGENT_THINK_IN_TOOL_CALL_CRITICAL,
    EVT_AGENT_TOOL_CALL_CYCLIC_CRITICAL,
    EVT_NUDGE,
    EVT_PLAN_CONFLICT_CRITICAL,
    EVT_PLAN_STATE,
    EVT_REVIEW_FINDINGS,
    EVT_SECURITY_RULE_ADDED,
    EVT_SESSION_GREETING,
)


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[Envelope] = []

    async def send(self, env: Envelope) -> None:
        self.sent.append(env)


def _make(tmp_path: Path) -> tuple[EngineEmitters, _FakeSink, TransientStore]:
    transient = TransientStore(tmp_path)
    transient.attach_session("sess-1", resumed=False)
    sink = _FakeSink()
    emitters = EngineEmitters(
        sink, SessionState(session_id="sess-1"), context_stats=lambda: {}, transient=transient
    )
    return emitters, sink, transient


def _strip(line: dict[str, object]) -> dict[str, object]:
    """Drop the store-stamped ``id``/``ts`` so markers compare by content."""
    return {k: v for k, v in line.items() if k not in ("id", "ts")}


_Emit = Callable[[EngineEmitters], Awaitable[None]]

# (emit call, expected event type, expected event payload, expected marker)
_CASES: list[tuple[str, _Emit, str, dict[str, object], dict[str, object]]] = [
    (
        "greeting",
        lambda e: e.emit_greeting("Hi there"),
        EVT_SESSION_GREETING,
        {"text": "Hi there"},
        {"type": "greeting", "text": "Hi there"},
    ),
    (
        "security_rule_added",
        lambda e: e.emit_security_rule_added("session", "git", "status"),
        EVT_SECURITY_RULE_ADDED,
        {"scope": "session", "executable": "git", "subcommand": "status"},
        {
            "type": "security_rule_added",
            "scope": "session",
            "executable": "git",
            "subcommand": "status",
        },
    ),
    (
        "stuck_critical",
        lambda e: e.emit_agent_stuck_critical("stalled twice"),
        EVT_AGENT_STUCK_CRITICAL,
        {"message": "stalled twice"},
        {"type": "agent_stuck_critical", "message": "stalled twice"},
    ),
    (
        "cyclic_thinking_critical",
        lambda e: e.emit_cyclic_thinking_critical("looping thoughts"),
        EVT_AGENT_CYCLIC_THINKING_CRITICAL,
        {"message": "looping thoughts"},
        {"type": "agent_cyclic_thinking_critical", "message": "looping thoughts"},
    ),
    (
        "think_in_tool_call_critical",
        lambda e: e.emit_think_in_tool_call_critical("think in call"),
        EVT_AGENT_THINK_IN_TOOL_CALL_CRITICAL,
        {"message": "think in call"},
        {"type": "agent_think_in_tool_call_critical", "message": "think in call"},
    ),
    (
        "tool_call_cyclic_critical",
        lambda e: e.emit_tool_call_cyclic_critical("same call again"),
        EVT_AGENT_TOOL_CALL_CYCLIC_CRITICAL,
        {"message": "same call again"},
        {"type": "agent_tool_call_cyclic_critical", "message": "same call again"},
    ),
    (
        "review_findings",
        lambda e: e.emit_review_findings(
            work_product_id="proj/coder",
            agent="coder",
            reviewer_name="code_critic",
            iteration=2,
            max_rounds=5,
            paths=["src/a.py"],
            findings=[{"id": "f1", "status": "open"}],
        ),
        EVT_REVIEW_FINDINGS,
        {
            "work_product_id": "proj/coder",
            "agent": "coder",
            "reviewer_name": "code_critic",
            "iteration": 2,
            "max_rounds": 5,
            "paths": ["src/a.py"],
            "findings": [{"id": "f1", "status": "open"}],
        },
        {
            "type": "review_findings",
            "work_product_id": "proj/coder",
            "agent": "coder",
            "reviewer_name": "code_critic",
            "iteration": 2,
            "max_rounds": 5,
            "paths": ["src/a.py"],
            "findings": [{"id": "f1", "status": "open"}],
        },
    ),
    (
        "plan_state",
        lambda e: e.emit_plan_state({"tasks": [], "current": 0}, "created", issue="dropped 1"),
        EVT_PLAN_STATE,
        {"reason": "created", "issue": "dropped 1", "tasks": [], "current": 0},
        {
            "type": "plan_state",
            "reason": "created",
            "issue": "dropped 1",
            "tasks": [],
            "current": 0,
        },
    ),
    (
        "plan_conflict_critical",
        lambda e: e.emit_plan_conflict_critical("re-plan with tasks outstanding"),
        EVT_PLAN_CONFLICT_CRITICAL,
        {"message": "re-plan with tasks outstanding"},
        {"type": "plan_conflict_critical", "message": "re-plan with tasks outstanding"},
    ),
]

_IDS = [case[0] for case in _CASES]
_PARAMS = [case[1:] for case in _CASES]


@pytest.mark.parametrize(("emit", "event_type", "event_payload", "marker"), _PARAMS, ids=_IDS)
async def test_emitter_pushes_event_and_persists_marker_to_main_log(
    tmp_path: Path,
    emit: _Emit,
    event_type: str,
    event_payload: dict[str, object],
    marker: dict[str, object],
) -> None:
    emitters, sink, transient = _make(tmp_path)

    await emit(emitters)

    assert [env.kind for env in sink.sent] == ["event"]
    assert sink.sent[0].payload == {"type": event_type, **event_payload}
    assert [_strip(line) for line in transient.read_session_lines()] == [marker]
    assert transient.read_messages() == []


@pytest.mark.parametrize(("emit", "event_type", "event_payload", "marker"), _PARAMS, ids=_IDS)
async def test_emitter_routes_marker_to_active_subsession(
    tmp_path: Path,
    emit: _Emit,
    event_type: str,
    event_payload: dict[str, object],
    marker: dict[str, object],
) -> None:
    emitters, sink, transient = _make(tmp_path)
    transient.update(active_subsession={"subsession_id": "sub-7", "agent": "coder"})

    await emit(emitters)

    assert sink.sent[0].payload["type"] == event_type
    assert transient.read_session_lines() == []
    assert [_strip(line) for line in transient.read_subsession_lines("sub-7")] == [marker]
    assert transient.read_subsession_messages("sub-7") == []


async def test_nudge_is_pushed_live_without_persisting_a_marker(tmp_path: Path) -> None:
    emitters, sink, transient = _make(tmp_path)

    await emitters.emit_nudge("Kodo nudged the agent", ["looping"], "soft", "post_round")

    assert len(sink.sent) == 1
    assert sink.sent[0].payload == {
        "type": EVT_NUDGE,
        "ui_text": "Kodo nudged the agent",
        "reasons": ["looping"],
        "mode": "soft",
        "source": "post_round",
    }
    assert transient.read_session_lines() == []


async def test_plan_state_issue_defaults_to_empty(tmp_path: Path) -> None:
    emitters, sink, _ = _make(tmp_path)

    await emitters.emit_plan_state({"tasks": []}, "read")

    assert sink.sent[0].payload == {
        "type": EVT_PLAN_STATE,
        "reason": "read",
        "issue": "",
        "tasks": [],
    }

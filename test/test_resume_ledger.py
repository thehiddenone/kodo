"""Crash-resume replay-ledger reconstruction (WorkflowEngine.__build_replay_ledger).

When a main turn is interrupted while a sub-agent held the floor, resume rebuilds
an ordered ledger of the subsessions recorded after the last persisted assistant
message. A subsession paired with a ``subsession_end`` is ``completed`` and its
stored structured result must survive into the ledger verbatim, so the parent
agent receives the sub-agent's real output on resume instead of an empty stub.
"""

from __future__ import annotations

import json

from kodo.llms import Message
from kodo.runtime import WorkflowEngine


class _StubTransient:
    """Minimal stand-in exposing only what __build_replay_ledger reads."""

    def __init__(self, lines: list[dict[str, object]]) -> None:
        self._lines = lines

    def read_session_lines(self) -> list[dict[str, object]]:
        return self._lines


def _ledger_for(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    """Run __build_replay_ledger against canned session lines, bypassing __init__."""
    engine = object.__new__(WorkflowEngine)
    engine._WorkflowEngine__transient = _StubTransient(lines)  # type: ignore[attr-defined]
    return engine._WorkflowEngine__build_replay_ledger()  # type: ignore[attr-defined]


def test_completed_subsession_preserves_structured_dict_result() -> None:
    """A dict (the standard return_result shape) survives into the ledger verbatim.

    Regression: the builder previously kept only list-shaped (legacy artifact-id)
    results and coerced everything else to ``[]``. A completed sub-agent's real
    output is a dict, so it was discarded — the parent then received an empty
    ``{"artifact_ids": [], schema_compliance: False}`` on resume and lost the
    sub-agent's work.
    """
    result = {
        "scripts_created": ["scripts/build.sh"],
        "summary": "done",
        "schema_compliance": True,
    }
    lines: list[dict[str, object]] = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "run_subagent"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "toolchain_python"},
        {
            "type": "subsession_end",
            "subsession_id": "s1",
            "agent": "toolchain_python",
            "failed": False,
            "result": result,
        },
    ]
    ledger = _ledger_for(lines)
    assert len(ledger) == 1
    assert ledger[0]["completed"] is True
    assert ledger[0]["result"] == result


def test_completed_subsession_preserves_legacy_list_result() -> None:
    """A bare artifact-id list (legacy marker shape) is still carried through."""
    lines: list[dict[str, object]] = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "run_subagent"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "coder"},
        {
            "type": "subsession_end",
            "subsession_id": "s1",
            "agent": "coder",
            "result": ["artifact-1", "artifact-2"],
        },
    ]
    ledger = _ledger_for(lines)
    assert ledger[0]["completed"] is True
    assert ledger[0]["result"] == ["artifact-1", "artifact-2"]


def test_active_unclosed_subsession_is_incomplete_with_no_result() -> None:
    """An unpaired start (the in-flight subsession at crash) is driven live, not reused."""
    lines: list[dict[str, object]] = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "run_subagent"}]},
        {"type": "subsession_start", "subsession_id": "s1", "agent": "toolchain_python"},
    ]
    ledger = _ledger_for(lines)
    assert len(ledger) == 1
    assert ledger[0]["completed"] is False
    assert ledger[0]["result"] == {}


def test_only_markers_after_last_assistant_count() -> None:
    """Markers from an earlier, already-handed-back subsession are ignored."""
    lines: list[dict[str, object]] = [
        {"type": "subsession_start", "subsession_id": "old", "agent": "coder"},
        {"type": "subsession_end", "subsession_id": "old", "agent": "coder", "result": {"a": 1}},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "run_subagent"}]},
        {"type": "subsession_start", "subsession_id": "new", "agent": "toolchain_python"},
        {
            "type": "subsession_end",
            "subsession_id": "new",
            "agent": "toolchain_python",
            "result": {"summary": "fresh", "schema_compliance": True},
        },
    ]
    ledger = _ledger_for(lines)
    assert [entry["subsession_id"] for entry in ledger] == ["new"]
    assert ledger[0]["result"] == {"summary": "fresh", "schema_compliance": True}


def _engine_with_messages(messages: list[Message]) -> WorkflowEngine:
    """A WorkflowEngine with only ``__main_messages`` seeded, bypassing __init__."""
    engine = object.__new__(WorkflowEngine)
    engine._WorkflowEngine__main_messages = messages  # type: ignore[attr-defined]
    return engine


def test_dangling_tool_use_detected_for_non_spawn_tool() -> None:
    """A non-spawn tool cut off mid-dispatch is now a resumable dangling turn.

    Every tool-calling turn flushes its assistant ``tool_use`` before dispatch,
    so an interrupted ``run_command`` (not just a sub-agent spawn) leaves the
    dangling assistant message resume must resolve.
    """
    engine = _engine_with_messages(
        [
            Message(role="user", content="do it"),
            Message(
                role="assistant",
                content=[{"type": "tool_use", "id": "t1", "name": "run_command"}],
            ),
        ]
    )
    assert engine._WorkflowEngine__has_dangling_tool_use() is True  # type: ignore[attr-defined]


def test_no_dangling_when_tool_result_present() -> None:
    """A completed tool call (result already persisted) is not a resumable turn."""
    engine = _engine_with_messages(
        [
            Message(
                role="assistant",
                content=[{"type": "tool_use", "id": "t1", "name": "run_command"}],
            ),
            Message(role="user", content=[{"type": "tool_result", "tool_use_id": "t1"}]),
        ]
    )
    assert engine._WorkflowEngine__has_dangling_tool_use() is False  # type: ignore[attr-defined]


def test_interrupted_tool_result_is_a_failure_envelope() -> None:
    """The stand-in result for a non-re-executed tool is a well-formed error block.

    Resume must not re-run an arbitrary interrupted tool (its side effects may
    already have landed), so it hands the model an ``error`` envelope keyed to
    the original ``tool_use_id`` instead — read back as a failure and rendered
    with a failure badge.
    """
    block = WorkflowEngine._WorkflowEngine__interrupted_tool_result(  # type: ignore[attr-defined]
        "t1", "run_command"
    )
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "t1"
    payload = json.loads(block["content"])
    assert "error" in payload
    assert "run_command" in payload["error"]

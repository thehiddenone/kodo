"""Behavior tests for folding a user-initiated Stop into session.jsonl.

Exercises ``WorkflowEngine._persist_interrupted_turn`` (the dangling-tool_use
resolution + LLM-visible "you were stopped" notice appended by ``stop()``) and
``WorkflowEngine._partial_assistant_message`` (how a stream cut short by
Stop is folded into a real, persisted assistant message) directly, using the
same ``object.__new__(WorkflowEngine)`` + minimal-stub pattern as
``test_engine_document_flow.py`` — both are private engine methods with no
public surface.
"""

from __future__ import annotations

import pytest

from kodo.llms import Message, ToolCallEvent
from kodo.runtime import WorkflowEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTransient:
    def __init__(self, pending_security_alert: str | None = None) -> None:
        self.appended: list[tuple[str, object, str | None, str | None]] = []
        self.pending_security_alert = pending_security_alert
        self.update_calls: list[dict[str, object]] = []

    def append_message(
        self,
        role: str,
        content: object,
        entry_agent: str | None = None,
        attachments: object = None,
        kind: str | None = None,
    ) -> None:
        self.appended.append((role, content, entry_agent, kind))

    def update(self, **kwargs: object) -> None:
        self.update_calls.append(kwargs)
        if "pending_security_alert" in kwargs:
            self.pending_security_alert = kwargs["pending_security_alert"]


def _bare_engine(
    *, main_messages: list[Message], pending_security_alert: str | None = None
) -> tuple[WorkflowEngine, _FakeTransient]:
    """Construct a WorkflowEngine with only the attributes these methods read."""
    engine = object.__new__(WorkflowEngine)
    transient = _FakeTransient(pending_security_alert=pending_security_alert)
    engine._main_messages = main_messages
    engine._transient = transient
    return engine, transient


# ---------------------------------------------------------------------------
# _persist_interrupted_turn
# ---------------------------------------------------------------------------


def test_persist_interrupted_turn_with_dangling_tool_use_synthesizes_result() -> None:
    """A Stop mid tool-dispatch gets a synthesized tool_result + the notice."""
    dangling = Message(
        role="assistant",
        content=[
            {"type": "text", "text": "Let me check that."},
            {"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {"command": "ls"}},
        ],
    )
    engine, transient = _bare_engine(main_messages=[Message(role="user", content="go"), dangling])

    engine._persist_interrupted_turn("guide")

    main = engine._main_messages
    assert [m.role for m in main[-2:]] == ["user", "assistant"]

    tool_results_msg = main[-2]
    assert isinstance(tool_results_msg.content, list)
    assert len(tool_results_msg.content) == 1
    result_block = tool_results_msg.content[0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "tu_1"
    assert "did not complete before the user clicked Stop" in result_block["content"]
    assert "run_command" in result_block["content"]

    notice_msg = main[-1]
    assert "The ongoing session was interrupted by the user" in notice_msg.content

    # Both new messages were persisted, tagged with the entry agent that was
    # actually running, and in the right order; only the notice carries
    # kind="stopped_notice" (so history replay renders it as the red callout,
    # not a fake user-typed bubble — see HistoryProjector._message_to_entries).
    assert [role for role, _content, _agent, _kind in transient.appended] == ["user", "assistant"]
    assert all(agent == "guide" for _role, _content, agent, _kind in transient.appended)
    assert transient.appended[0] == ("user", tool_results_msg.content, "guide", None)
    assert transient.appended[1] == ("assistant", notice_msg.content, "guide", "stopped_notice")


def test_persist_interrupted_turn_without_dangling_tool_use_only_adds_notice() -> None:
    """A Stop mid-stream (no tool call in flight) just gets the notice appended."""
    plain_reply = Message(role="assistant", content="Here's what I found so far...")
    engine, transient = _bare_engine(
        main_messages=[Message(role="user", content="go"), plain_reply]
    )

    engine._persist_interrupted_turn("problem_solver")

    main = engine._main_messages
    assert len(main) == 3
    assert main[-1].role == "assistant"
    assert "The ongoing session was interrupted by the user" in main[-1].content

    assert transient.appended == [
        ("assistant", main[-1].content, "problem_solver", "stopped_notice")
    ]


def test_persist_interrupted_turn_resolves_every_pending_tool_use() -> None:
    """A batch tool call (parallel tool_use blocks) gets a result for each."""
    dangling = Message(
        role="assistant",
        content=[
            {"type": "tool_use", "id": "tu_1", "name": "filesystem", "input": {}},
            {"type": "tool_use", "id": "tu_2", "name": "run_command", "input": {}},
        ],
    )
    engine, _transient = _bare_engine(main_messages=[dangling])

    engine._persist_interrupted_turn("guide")

    tool_results_msg = engine._main_messages[-2]
    ids = {block["tool_use_id"] for block in tool_results_msg.content}
    assert ids == {"tu_1", "tu_2"}


def test_persist_interrupted_turn_clears_stale_pending_security_alert() -> None:
    """A live Stop never redispatches a gate-pending call — same "I will not
    silently resume or retry it" rule as any other dangling call — but must
    still clear the marker so it cannot outlive the call it pointed at and
    linger, unmatched, into a later cold-restart resume."""
    dangling = Message(
        role="assistant",
        content=[{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {}}],
    )
    engine, transient = _bare_engine(main_messages=[dangling], pending_security_alert="tu_1")

    engine._persist_interrupted_turn("guide")

    assert transient.pending_security_alert is None
    # The call is still folded into an ordinary interrupted result, not
    # silently redispatched.
    tool_results_msg = engine._main_messages[-2]
    assert tool_results_msg.content[0]["tool_use_id"] == "tu_1"
    assert "did not complete before the user clicked Stop" in tool_results_msg.content[0]["content"]


def test_persist_interrupted_turn_noop_when_no_pending_security_alert() -> None:
    """No marker to begin with -> no spurious update() call for it."""
    plain_reply = Message(role="assistant", content="partial")
    engine, transient = _bare_engine(main_messages=[plain_reply])

    engine._persist_interrupted_turn("guide")

    assert transient.pending_security_alert is None
    assert not any("pending_security_alert" in call for call in transient.update_calls)


# ---------------------------------------------------------------------------
# History replay: kind="stopped_notice" must not become a fake user bubble
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopped_notice_replays_as_interrupted_not_user_message() -> None:
    """A reload must show the same red callout, never a fake user-typed line."""
    from pathlib import Path

    from kodo.runtime._engine._checkpointing import CheckpointCoordinator
    from kodo.runtime._engine._history import HistoryProjector

    checkpoints = object.__new__(CheckpointCoordinator)
    projector = HistoryProjector(_FakeTransient(), checkpoints)  # type: ignore[arg-type]
    line = {
        "role": "user",
        "content": "The user clicked Stop, cutting the previous turn short...",
        "kind": "stopped_notice",
    }

    entries = await projector._message_to_entries(
        line, {}, {}, Path("/nonexistent"), Path("/nonexistent"), {}
    )

    assert entries == [{"type": "interrupted"}]


# ---------------------------------------------------------------------------
# _partial_assistant_message
# ---------------------------------------------------------------------------


def _method(engine: WorkflowEngine):
    return engine._partial_assistant_message


def test_partial_assistant_message_returns_none_when_nothing_arrived() -> None:
    engine, _ = _bare_engine(main_messages=[])
    assert _method(engine)([], [], None, []) is None


def test_partial_assistant_message_captures_text_only() -> None:
    engine, _ = _bare_engine(main_messages=[])
    msg = _method(engine)(["Hello, ", "world"], [], None, [])
    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content == [{"type": "text", "text": "Hello, world"}]


def test_partial_assistant_message_captures_thinking_and_tool_calls() -> None:
    engine, _ = _bare_engine(main_messages=[])
    tool_call = ToolCallEvent(tool_use_id="tu_9", tool_name="filesystem", tool_input={"a": 1})
    msg = _method(engine)(["partial text"], ["thinking..."], "sig-abc", [tool_call])
    assert msg is not None
    assert msg.content[0] == {"type": "thinking", "thinking": "thinking...", "signature": "sig-abc"}
    assert msg.content[1] == {"type": "text", "text": "partial text"}
    assert msg.content[2] == {
        "type": "tool_use",
        "id": "tu_9",
        "name": "filesystem",
        "input": {"a": 1},
    }


# ---------------------------------------------------------------------------
# _interrupted_tool_result reason wording
# ---------------------------------------------------------------------------


def test_interrupted_tool_result_reason_selects_wording() -> None:
    engine, _ = _bare_engine(main_messages=[])
    restart = engine._interrupted_tool_result("tu_1", "run_command")
    stopped = engine._interrupted_tool_result("tu_1", "run_command", reason="stopped")
    assert "server restart or window reload" in restart["content"]
    assert "user clicked Stop" in stopped["content"]

"""Tests for ``kodo.runtime._engine._turns.TurnLoopMixin`` — entry-agent runs
and the generic LLM turn/tool loop.

``_partial_assistant_message``/``_thinking_block``/``_interrupted_tool_result``
already have focused coverage in ``test_engine_stop.py``; this file covers
the rest: the three entry-agent delegators, ``_run_entry_agent``,
``_store_attachments``, the core ``_run_agent_turn`` loop (streaming, tool
dispatch, cancellation, context tracking), ``_dispatch_tool_calls``,
``_finalize_tool_result``, and ``_make_dispatcher``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodo.llms import (
    LLMRouting,
    Message,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    ToolCallLogger,
    TurnEnd,
    Usage,
)
from kodo.runtime import WorkflowEngine
from kodo.runtime._checkpoints import CheckpointRef, CheckpointState
from kodo.runtime._session import SessionState
from kodo.tools import ToolDispatcher

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGateway:
    def __init__(self, batches: list[list[object] | Exception]) -> None:
        self._batches = list(batches)
        self.calls: list[dict[str, object]] = []

    async def stream_query(self, **kwargs: object):
        self.calls.append(kwargs)
        batch = self._batches.pop(0) if self._batches else []
        if isinstance(batch, BaseException):
            raise batch
        for event in batch:
            yield event


class _FakeEmitters:
    def __init__(self) -> None:
        self.stream_events: list[tuple[object, str]] = []
        self.cost_total = 0.0
        self.usage_calls: list[tuple[object, str, float, str]] = []
        self.context_stats_calls = 0
        self.state_emits = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.errors: list[tuple[str, bool]] = []

    async def handle_stream_event(self, event: object, stream_id: str) -> None:
        self.stream_events.append((event, stream_id))

    def add_cost(self, usd: float) -> None:
        self.cost_total += usd

    @property
    def cumulative_usd(self) -> float:
        return self.cost_total

    async def emit_usage(
        self, turn_end: object, model: str, duration: float, agent_name: str
    ) -> None:
        self.usage_calls.append((turn_end, model, duration, agent_name))

    async def emit_context_stats(self) -> None:
        self.context_stats_calls += 1

    async def emit_state(self) -> None:
        self.state_emits += 1

    async def emit_agent_started(self, name: str) -> None:
        self.started.append(name)

    async def emit_agent_finished(self, name: str) -> None:
        self.finished.append(name)

    async def emit_error(self, message: str, *, recoverable: bool) -> None:
        self.errors.append((message, recoverable))


class _FakeTransient:
    def __init__(self) -> None:
        self.appended: list[tuple[str, object, str | None, object]] = []
        self.stored: dict[str, str] = {}
        self.tool_calls_written: list[tuple[str, str]] = []
        self.diffs_written: list[dict[str, object]] = []
        self._store_result: tuple[str, str] | None = ("attach-1", "attachments/a1.txt")

    def append_message(
        self, role, content, entry_agent=None, attachments=None, kind=None, detail=None
    ) -> None:
        self.appended.append((role, content, entry_agent, attachments))

    def store_attachment(self, display_name: str, content: str) -> tuple[str, str] | None:
        return self._store_result

    def attachment_abs_path(self, stored_rel: str) -> str:
        return f"/abs/{stored_rel}"

    def write_tool_call(self, tool_use_id: str, markdown: str) -> Path | None:
        self.tool_calls_written.append((tool_use_id, markdown))
        return Path(f"/tool_calls/{tool_use_id}.md")

    def write_diff_files(self, tool_use_id, *, label, filename, old_content, new_content):
        self.diffs_written.append(
            {"tool_use_id": tool_use_id, "label": label, "filename": filename}
        )
        return {"label": label, "prev_path": "/prev", "new_path": "/new"}


class _FakeMirrors:
    def __init__(self, states: dict[str, CheckpointState] | None = None) -> None:
        self.states = states or {}

    async def state_for(self, root: str) -> CheckpointState:
        return self.states.get(root, CheckpointState())


class _FakeCheckpoints:
    def __init__(self, *, prepare_paths=None, commit_ref: CheckpointRef | None = None) -> None:
        self.mirrors = _FakeMirrors()
        self._prepare_paths = prepare_paths or []
        self._commit_ref = commit_ref
        self.prepared: list[tuple[str, dict[str, object]]] = []
        self.committed: list[tuple[str, dict[str, object], list[Path]]] = []
        self.pushed: list[tuple[str, object]] = []
        self.guided_revisions: list[tuple[str, dict[str, object], object, str]] = []

    async def prepare(self, tool_name, tool_input):
        self.prepared.append((tool_name, tool_input))
        return list(self._prepare_paths)

    async def commit(self, tool_name, tool_input, paths):
        self.committed.append((tool_name, tool_input, paths))
        return self._commit_ref

    async def push_state(self, root, state) -> None:
        self.pushed.append((root, state))

    async def record_guided_revision(self, tool_name, tool_input, checkpoint, agent_name) -> None:
        self.guided_revisions.append((tool_name, tool_input, checkpoint, agent_name))


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


class _FakeCompactor:
    def __init__(self) -> None:
        self.context_tokens = 0
        self.noted: list[str] = []
        self.auto_compact_calls = 0

    def note_active_model(self, key: str) -> None:
        self.noted.append(key)

    async def maybe_auto_compact(self) -> None:
        self.auto_compact_calls += 1


def _usage(**overrides: object) -> Usage:
    fields = dict(
        input_tokens=10, output_tokens=5, cache_write_tokens=1, cache_read_tokens=2, model="m"
    )
    fields.update(overrides)
    return Usage(**fields)  # type: ignore[arg-type]


def _base_engine(*, gateway: _FakeGateway | None = None) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._gateway = gateway or _FakeGateway([])
    engine._emitters = _FakeEmitters()
    engine._sink = _FakeSink()
    engine._transient = _FakeTransient()
    engine._checkpoints = _FakeCheckpoints()
    engine._compactor = _FakeCompactor()
    engine._session = SessionState()
    engine._orch_session_id = "sess-1"
    engine._entry_turn_seq = 0

    def _llm_logs_dir() -> Path:
        return Path("/tmp/llm_logs")

    engine._llm_logs_dir = _llm_logs_dir
    return engine


# ---------------------------------------------------------------------------
# Entry-agent delegators
# ---------------------------------------------------------------------------


async def test_run_guide_with_input_delegates() -> None:
    engine = object.__new__(WorkflowEngine)
    calls: list[tuple[str, str, list[str] | None]] = []

    async def _run_entry_agent(agent_name, text, attachments=None, nudge_detail=None):
        calls.append((agent_name, text, attachments))

    engine._run_entry_agent = _run_entry_agent
    await engine._run_guide_with_input("hello", ["a.png"])
    assert calls == [("guide", "hello", ["a.png"])]


async def test_run_problem_solver_with_input_delegates() -> None:
    engine = object.__new__(WorkflowEngine)
    calls = []

    async def _run_entry_agent(agent_name, text, attachments=None, nudge_detail=None):
        calls.append((agent_name, text, attachments))

    engine._run_entry_agent = _run_entry_agent
    await engine._run_problem_solver_with_input("fix it")
    assert calls == [("problem_solver", "fix it", None)]


async def test_run_judge_with_input_delegates() -> None:
    engine = object.__new__(WorkflowEngine)
    calls = []

    async def _run_entry_agent(agent_name, text, attachments=None, nudge_detail=None):
        calls.append((agent_name, text, attachments))

    engine._run_entry_agent = _run_entry_agent
    await engine._run_judge_with_input("score it")
    assert calls == [("judge", "score it", None)]


# ---------------------------------------------------------------------------
# _store_attachments
# ---------------------------------------------------------------------------


async def test_store_attachments_success(tmp_path: Path) -> None:
    engine = _base_engine()
    src = tmp_path / "notes.txt"
    src.write_text("hello world")

    stored, errors = await engine._store_attachments([str(src)])

    assert errors == []
    assert len(stored) == 1
    assert stored[0]["name"] == "notes.txt"
    assert stored[0]["id"] == "attach-1"
    assert stored[0]["stored"] == "attachments/a1.txt"


async def test_store_attachments_missing_file_reports_error(tmp_path: Path) -> None:
    engine = _base_engine()
    missing = tmp_path / "gone.txt"

    stored, errors = await engine._store_attachments([str(missing)])

    assert stored == []
    assert len(errors) == 1


async def test_store_attachments_caps_at_max_attachments(tmp_path: Path) -> None:
    engine = _base_engine()
    paths = []
    for i in range(10):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x")
        paths.append(str(p))

    stored, errors = await engine._store_attachments(paths)

    assert len(stored) == 9  # MAX_ATTACHMENTS
    assert any("At most 9 files" in e for e in errors)


async def test_store_attachments_storage_failure_reports_error(tmp_path: Path) -> None:
    engine = _base_engine()
    engine._transient._store_result = None
    src = tmp_path / "notes.txt"
    src.write_text("hi")

    stored, errors = await engine._store_attachments([str(src)])

    assert stored == []
    assert "could not be saved" in errors[0]


# ---------------------------------------------------------------------------
# _persist_main_messages
# ---------------------------------------------------------------------------


def test_persist_main_messages_appends_each_message() -> None:
    engine = _base_engine()
    persist = engine._persist_main_messages("guide")

    persist([Message(role="user", content="hi"), Message(role="assistant", content="hello")])

    assert engine._transient.appended == [
        ("user", "hi", "guide", None),
        ("assistant", "hello", "guide", None),
    ]


# ---------------------------------------------------------------------------
# _run_agent_turn
# ---------------------------------------------------------------------------


def _agent_turn_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        llm=SimpleNamespace(name="fake"),
        # Cloud residence: makes _thinking_kwargs() short-circuit to {} like
        # the bare SimpleNamespace() this used to pass, which _thinking_kwargs
        # now reads .residence off.
        routing=LLMRouting(residence="cloud"),
        model="model-x",
        system_prompt="sys",
        messages=[Message(role="user", content="go")],
        tools=[],
        stream_id="stream-1",
        agent_name="guide",
    )
    base.update(overrides)
    return base


async def test_run_agent_turn_no_tool_calls_text_only() -> None:
    events = [
        [
            TokenDelta(text="hello "),
            TokenDelta(text="world"),
            TurnEnd(usage=_usage(), stop_reason="end_turn"),
        ]
    ]
    engine = _base_engine(gateway=_FakeGateway(events))

    async def tool_dispatch(*a, **k):
        raise AssertionError("should not be called")

    messages, files = await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch)
    )

    assert messages[-1].role == "assistant"
    assert messages[-1].content == "hello world"
    assert files == []
    assert engine._emitters.usage_calls[0][1] == "model-x"
    assert engine._emitters.usage_calls[0][3] == "guide"


async def test_run_agent_turn_no_text_defaults_to_placeholder() -> None:
    engine = _base_engine(gateway=_FakeGateway([[TurnEnd(usage=_usage(), stop_reason="end_turn")]]))

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch)
    )

    assert messages[-1].content == "(no text)"


async def test_run_agent_turn_with_thinking_builds_thinking_block() -> None:
    events = [
        [
            ThinkingDelta(text="pondering"),
            ThinkingSignature(signature="sig-1"),
            TokenDelta(text="answer"),
            TurnEnd(usage=_usage(), stop_reason="end_turn"),
        ]
    ]
    engine = _base_engine(gateway=_FakeGateway(events))

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch)
    )

    assert messages[-1].content[0] == {
        "type": "thinking",
        "thinking": "pondering",
        "signature": "sig-1",
    }
    assert messages[-1].content[1] == {"type": "text", "text": "answer"}


async def test_run_agent_turn_dispatches_tool_calls_and_loops() -> None:
    round1 = [
        ToolCallEvent(tool_use_id="tu_1", tool_name="run_command", tool_input={"command": "ls"}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    round2 = [TokenDelta(text="done"), TurnEnd(usage=_usage(), stop_reason="end_turn")]
    engine = _base_engine(gateway=_FakeGateway([round1, round2]))

    dispatched: list[tuple[str, dict[str, object], str]] = []

    async def tool_dispatch(name, tool_input, tool_use_id, recovered=False):
        dispatched.append((name, tool_input, tool_use_id))
        return '{"exit_code": 0}'

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch)
    )

    assert dispatched == [("run_command", {"command": "ls"}, "tu_1")]
    # tool_use assistant msg + tool_result user msg + final assistant msg
    assert messages[-1].content == "done"
    assert messages[-3].content[-1]["type"] == "tool_use"
    assert messages[-2].content[0]["type"] == "tool_result"


async def test_run_agent_turn_tool_call_round_carries_thinking_and_text_blocks() -> None:
    round1 = [
        ThinkingDelta(text="considering options"),
        ThinkingSignature(signature="sig-9"),
        TokenDelta(text="I'll check that"),
        ToolCallEvent(tool_use_id="tu_1", tool_name="return_result", tool_input={"result": {}}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    engine = _base_engine(gateway=_FakeGateway([round1]))

    async def tool_dispatch(*a, **k):
        return "{}"

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch, stop_after_tools=lambda: True)
    )

    assistant_msg = messages[-2]
    assert assistant_msg.content[0] == {
        "type": "thinking",
        "thinking": "considering options",
        "signature": "sig-9",
    }
    assert assistant_msg.content[1] == {"type": "text", "text": "I'll check that"}
    assert assistant_msg.content[2]["type"] == "tool_use"


async def test_run_agent_turn_flush_before_dispatch_and_track_context_syncs_main_messages() -> None:
    round1 = [
        ToolCallEvent(tool_use_id="tu_1", tool_name="run_command", tool_input={}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    engine = _base_engine(gateway=_FakeGateway([round1]))
    seen_main_messages_at_dispatch: list[int] = []

    async def tool_dispatch(*a, **k):
        seen_main_messages_at_dispatch.append(len(engine._main_messages))
        return "{}"

    await engine._run_agent_turn(
        **_agent_turn_kwargs(
            tool_dispatch=tool_dispatch,
            flush_before_dispatch=True,
            track_context=True,
            stop_after_tools=lambda: True,
        )
    )

    # _main_messages already carried the dangling tool_use before dispatch ran.
    assert seen_main_messages_at_dispatch == [2]
    # And after the tool result comes back, it's synced again (post-dispatch).
    assert engine._main_messages[-1].content[0]["type"] == "tool_result"


async def test_run_agent_turn_stop_after_tools_exits_loop_without_second_call() -> None:
    round1 = [
        ToolCallEvent(tool_use_id="tu_1", tool_name="return_result", tool_input={"result": {}}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    gateway = _FakeGateway([round1])
    engine = _base_engine(gateway=gateway)

    async def tool_dispatch(*a, **k):
        return "{}"

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch, stop_after_tools=lambda: True)
    )

    assert len(gateway.calls) == 1
    assert messages[-1].content[0]["type"] == "tool_result"


async def test_run_agent_turn_flush_before_dispatch_persists_before_tool_runs() -> None:
    round1 = [
        ToolCallEvent(tool_use_id="tu_1", tool_name="run_command", tool_input={}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    round2 = [TurnEnd(usage=_usage(), stop_reason="end_turn")]
    engine = _base_engine(gateway=_FakeGateway([round1, round2]))
    persisted_at_dispatch_time: list[int] = []

    async def tool_dispatch(*a, **k):
        persisted_at_dispatch_time.append(len(engine._transient.appended))
        return "{}"

    persisted_batches: list[list[Message]] = []

    def persist(batch: list[Message]) -> None:
        persisted_batches.append(batch)
        for m in batch:
            engine._transient.appended.append((m.role, m.content, "guide", None))

    await engine._run_agent_turn(
        **_agent_turn_kwargs(
            tool_dispatch=tool_dispatch, persist=persist, flush_before_dispatch=True
        )
    )

    # The assistant tool_use message was flushed before dispatch ran.
    assert persisted_at_dispatch_time == [1]
    assert persisted_batches[0][0].role == "assistant"


async def test_run_agent_turn_track_context_updates_compactor() -> None:
    engine = _base_engine(
        gateway=_FakeGateway(
            [
                [
                    TurnEnd(
                        usage=_usage(
                            input_tokens=100,
                            output_tokens=50,
                            cache_write_tokens=5,
                            cache_read_tokens=3,
                        ),
                        stop_reason="end_turn",
                    )
                ]
            ]
        )
    )

    async def tool_dispatch(*a, **k):
        raise AssertionError

    await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch, track_context=True)
    )

    assert engine._compactor.context_tokens == 100 + 3 + 5 + 50
    assert engine._emitters.context_stats_calls == 1


async def test_run_agent_turn_no_turn_end_skips_usage_and_context() -> None:
    engine = _base_engine(gateway=_FakeGateway([[TokenDelta(text="x")]]))

    async def tool_dispatch(*a, **k):
        raise AssertionError

    await engine._run_agent_turn(
        **_agent_turn_kwargs(tool_dispatch=tool_dispatch, track_context=True)
    )

    assert engine._emitters.usage_calls == []
    assert engine._emitters.context_stats_calls == 0


async def test_run_agent_turn_cancelled_mid_stream_persists_partial_when_track_context() -> None:
    engine = _base_engine(gateway=_FakeGateway([asyncio.CancelledError()]))

    async def tool_dispatch(*a, **k):
        raise AssertionError

    with pytest.raises(asyncio.CancelledError):
        await engine._run_agent_turn(
            **_agent_turn_kwargs(
                tool_dispatch=tool_dispatch,
                track_context=True,
                messages=[Message(role="user", content="go")],
            )
        )

    # stream_end was sent even though the call was cancelled.
    assert any(env.kind == "stream_end" for env in engine._sink.sent)


async def test_run_agent_turn_cancelled_after_partial_content_persists_it() -> None:
    class _PartialThenCancel:
        async def stream_query(self, **kwargs):
            yield TokenDelta(text="partial reply")
            raise asyncio.CancelledError()

    engine = _base_engine()
    engine._gateway = _PartialThenCancel()

    async def tool_dispatch(*a, **k):
        raise AssertionError

    persisted: list[list[Message]] = []

    def persist(batch: list[Message]) -> None:
        persisted.append(list(batch))

    original = [Message(role="user", content="go")]
    with pytest.raises(asyncio.CancelledError):
        await engine._run_agent_turn(
            **_agent_turn_kwargs(
                tool_dispatch=tool_dispatch, track_context=True, messages=original, persist=persist
            )
        )

    assert persisted[0][0].content == [{"type": "text", "text": "partial reply"}]
    assert engine._main_messages[-1].content == [{"type": "text", "text": "partial reply"}]


async def test_run_agent_turn_cancelled_with_nothing_arrived_leaves_messages_unpersisted() -> None:
    class _EmptyThenCancel:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_query(self, **kwargs):
            self.calls += 1
            raise asyncio.CancelledError()
            yield  # pragma: no cover - never reached, makes this an async generator

    engine = _base_engine()
    engine._gateway = _EmptyThenCancel()

    async def tool_dispatch(*a, **k):
        raise AssertionError

    original_messages = [Message(role="user", content="go")]
    with pytest.raises(asyncio.CancelledError):
        await engine._run_agent_turn(
            **_agent_turn_kwargs(
                tool_dispatch=tool_dispatch, track_context=True, messages=original_messages
            )
        )
    assert engine._main_messages == original_messages


async def test_run_agent_turn_cancelled_without_track_context_does_not_touch_main_messages() -> (
    None
):
    engine = _base_engine(gateway=_FakeGateway([asyncio.CancelledError()]))

    async def tool_dispatch(*a, **k):
        raise AssertionError

    with pytest.raises(asyncio.CancelledError):
        await engine._run_agent_turn(
            **_agent_turn_kwargs(tool_dispatch=tool_dispatch, track_context=False)
        )

    assert not hasattr(engine, "_main_messages")


async def test_run_agent_turn_generic_exception_sends_stream_end_and_reraises() -> None:
    engine = _base_engine(gateway=_FakeGateway([RuntimeError("boom")]))

    async def tool_dispatch(*a, **k):
        raise AssertionError

    with pytest.raises(RuntimeError, match="boom"):
        await engine._run_agent_turn(**_agent_turn_kwargs(tool_dispatch=tool_dispatch))

    assert any(env.kind == "stream_end" for env in engine._sink.sent)


# ---------------------------------------------------------------------------
# _dispatch_tool_calls
# ---------------------------------------------------------------------------


def _tool_logger(tmp_path: Path) -> ToolCallLogger:
    return ToolCallLogger(tmp_path)


async def test_dispatch_tool_calls_sends_prep_event_for_normal_tool(tmp_path: Path) -> None:
    engine = _base_engine()

    async def tool_dispatch(name, tool_input, tool_use_id, recovered):
        return "{}"

    results = await engine._dispatch_tool_calls(
        [("tu_1", "run_command", {"command": "ls", "timeout": 30})],
        tool_dispatch,
        {"run_command": "Run a command"},
        _tool_logger(tmp_path),
        "guide",
    )

    prep_events = [e for e in engine._sink.sent if e.payload.get("type") == "agent.tool_call_prep"]
    assert len(prep_events) == 1
    assert prep_events[0].payload["timeout_seconds"] == 30
    assert results[0]["type"] == "tool_result"
    assert results[0]["tool_use_id"] == "tu_1"


async def test_dispatch_tool_calls_suppresses_prep_event_for_ask_user(tmp_path: Path) -> None:
    engine = _base_engine()

    async def tool_dispatch(name, tool_input, tool_use_id, recovered):
        return "{}"

    await engine._dispatch_tool_calls(
        [("tu_1", "ask_user", {"questions": []})],
        tool_dispatch,
        {},
        _tool_logger(tmp_path),
        "guide",
    )

    prep_events = [e for e in engine._sink.sent if e.payload.get("type") == "agent.tool_call_prep"]
    assert prep_events == []


async def test_dispatch_tool_calls_web_search_defaults_timeout(tmp_path: Path) -> None:
    engine = _base_engine()

    async def tool_dispatch(name, tool_input, tool_use_id, recovered):
        return "{}"

    await engine._dispatch_tool_calls(
        [("tu_1", "web_search", {"query": "x"})],
        tool_dispatch,
        {},
        _tool_logger(tmp_path),
        "guide",
    )

    from kodo.runtime._engine._subagents import _DEFAULT_WEB_SEARCH_TIMEOUT_S

    assert engine._sink.sent[0].payload["timeout_seconds"] == _DEFAULT_WEB_SEARCH_TIMEOUT_S


async def test_dispatch_tool_calls_passes_recovered_flag(tmp_path: Path) -> None:
    engine = _base_engine()
    seen: list[bool] = []

    async def tool_dispatch(name, tool_input, tool_use_id, recovered):
        seen.append(recovered)
        return "{}"

    await engine._dispatch_tool_calls(
        [("tu_1", "run_command", {}), ("tu_2", "run_command", {})],
        tool_dispatch,
        {},
        _tool_logger(tmp_path),
        "guide",
        recovered_ids={"tu_1"},
    )

    assert seen == [True, False]


async def test_dispatch_tool_calls_uses_checkpoint_coordinator(tmp_path: Path) -> None:
    ref = CheckpointRef(root="root1", sha="abc", parent="def")
    engine = _base_engine()
    engine._checkpoints = _FakeCheckpoints(prepare_paths=[Path("/root/a.txt")], commit_ref=ref)

    async def tool_dispatch(name, tool_input, tool_use_id, recovered):
        return "{}"

    await engine._dispatch_tool_calls(
        [("tu_1", "edit_file", {"path": "a.txt"})],
        tool_dispatch,
        {},
        _tool_logger(tmp_path),
        "guide",
    )

    assert engine._checkpoints.prepared == [("edit_file", {"path": "a.txt"})]
    assert engine._checkpoints.committed[0][0] == "edit_file"


# ---------------------------------------------------------------------------
# _finalize_tool_result
# ---------------------------------------------------------------------------


async def test_finalize_tool_result_unknown_tool_passthrough() -> None:
    engine = _base_engine()
    result = await engine._finalize_tool_result("tu_1", "not_a_real_tool", {}, "raw text")
    assert result == "raw text"


async def test_finalize_tool_result_invalid_json_wraps_as_result_field() -> None:
    engine = _base_engine()
    result = await engine._finalize_tool_result(
        "tu_1", "run_command", {"command": "ls"}, "not json"
    )
    # run_command's schema has no "result" field, so the wrapped raw text is
    # dropped by normalization — but it must not crash, and compliance goes False.
    import json

    parsed = json.loads(result)
    assert parsed["schema_compliance"] is False


async def test_finalize_tool_result_normalizes_and_writes_markdown() -> None:
    engine = _base_engine()
    import json

    output = {"exit_code": 0, "stdout": "ok", "stderr": ""}
    result = await engine._finalize_tool_result(
        "tu_1", "run_command", {"command": "ls"}, json.dumps(output)
    )
    parsed = json.loads(result)
    assert parsed["exit_code"] == 0
    assert len(engine._transient.tool_calls_written) == 1

    detail_events = [
        e for e in engine._sink.sent if e.payload.get("type") == "agent.tool_call_detail"
    ]
    assert len(detail_events) == 1
    assert detail_events[0].payload["success"] is True


async def test_finalize_tool_result_ask_user_skips_detail_event() -> None:
    engine = _base_engine()
    import json

    await engine._finalize_tool_result(
        "tu_1", "ask_user", {"questions": []}, json.dumps({"answers": []})
    )
    detail_events = [
        e for e in engine._sink.sent if e.payload.get("type") == "agent.tool_call_detail"
    ]
    assert detail_events == []


async def test_finalize_tool_result_injects_checkpoint_sha_and_pushes_state() -> None:
    import json

    ref = CheckpointRef(root="root1", sha="abc123", parent="parent1")
    engine = _base_engine()
    output = {"status": "deleted", "operation": "delete_file", "path": "a.txt"}

    result = await engine._finalize_tool_result(
        "tu_1",
        "filesystem",
        {"operation": "delete_file", "path": "a.txt"},
        json.dumps(output),
        checkpoint=ref,
    )
    parsed = json.loads(result)
    assert parsed["checkpoint_sha"] == "abc123"
    assert parsed["checkpoint_root"] == "root1"
    assert engine._checkpoints.pushed[0][0] == "root1"


async def test_finalize_tool_result_pops_diff_before_normalizing() -> None:
    import json

    engine = _base_engine()
    output = {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "diff": {"label": "edit", "filename": "a.txt", "old_content": "a", "new_content": "b"},
    }
    result = await engine._finalize_tool_result(
        "tu_1", "run_command", {"command": "x"}, json.dumps(output)
    )
    assert "diff" not in json.loads(result)
    assert len(engine._transient.diffs_written) == 1


async def test_finalize_tool_result_records_guided_revision_for_guided_state_tool() -> None:
    import json

    ref = CheckpointRef(root="root1", sha="sha1", parent="parent1")
    engine = _base_engine()
    output = {"status": "deleted", "operation": "delete_file", "path": "a.txt"}

    await engine._finalize_tool_result(
        "tu_1",
        "filesystem",
        {"operation": "delete_file", "path": "a.txt"},
        json.dumps(output),
        checkpoint=ref,
        agent_name="architect",
    )

    assert engine._checkpoints.guided_revisions[0][0] == "filesystem"
    assert engine._checkpoints.guided_revisions[0][3] == "architect"


async def test_finalize_tool_result_document_feedback_accept_finalizes_document() -> None:
    import json

    engine = _base_engine()
    finalized: list[str] = []

    async def _finalize_document(path: str) -> None:
        finalized.append(path)

    engine._finalize_document = _finalize_document
    output = {"status": "recorded", "path": "specs/a.md"}

    await engine._finalize_tool_result(
        "tu_1", "document_feedback", {"path": "specs/a.md", "accept": True}, json.dumps(output)
    )

    assert finalized == ["specs/a.md"]


async def test_finalize_tool_result_document_feedback_reject_does_not_finalize() -> None:
    import json

    engine = _base_engine()
    finalized: list[str] = []

    async def _finalize_document(path: str) -> None:
        finalized.append(path)

    engine._finalize_document = _finalize_document
    output = {"status": "recorded", "path": "specs/a.md"}

    await engine._finalize_tool_result(
        "tu_1",
        "document_feedback",
        {"path": "specs/a.md", "accept": False, "concerns": [{"kind": "x", "description": "y"}]},
        json.dumps(output),
    )

    assert finalized == []


async def test_finalize_tool_result_noncompliant_output_emits_incompliant_event() -> None:
    import json

    engine = _base_engine()
    # Missing required "operation" field -> non-compliant per output_schema.
    output = {"status": "deleted"}

    await engine._finalize_tool_result(
        "tu_1", "filesystem", {"operation": "delete_file", "path": "a.txt"}, json.dumps(output)
    )

    incompliant_events = [
        e for e in engine._sink.sent if e.payload.get("type") == "tool.incompliant"
    ]
    assert len(incompliant_events) == 1


# ---------------------------------------------------------------------------
# _make_dispatcher
# ---------------------------------------------------------------------------


def test_make_dispatcher_builds_real_tool_dispatcher() -> None:
    engine = object.__new__(WorkflowEngine)
    engine._registry = SimpleNamespace(spec_for=lambda name: None)
    engine._make_resolver = lambda session_id: SimpleNamespace(
        resolve=lambda p: Path(p), default_cwd=Path(".")
    )
    engine._gate = SimpleNamespace()
    engine._security = None
    engine._session = SessionState(session_id="s1")
    engine._services = SimpleNamespace()
    engine._current_project = None
    engine._root_paths = lambda: ()
    engine._util_paths = lambda: {}

    dispatcher = engine._make_dispatcher("guide", "session-1")

    assert isinstance(dispatcher, ToolDispatcher)
    assert dispatcher.stop_requested is False
    assert dispatcher.returned_output is None


def test_make_dispatcher_resolves_project_root_from_current_project() -> None:
    engine = object.__new__(WorkflowEngine)
    engine._registry = SimpleNamespace(
        spec_for=lambda name: SimpleNamespace(output_schema={"type": "object"})
    )
    engine._make_resolver = lambda session_id: SimpleNamespace(
        resolve=lambda p: Path(p), default_cwd=Path(".")
    )
    engine._gate = SimpleNamespace()
    engine._security = None
    engine._session = SessionState(session_id="s1")
    engine._session.effective_workflow_mode = "guided"
    engine._services = SimpleNamespace()
    engine._current_project = {"root": "/proj", "name": "x"}
    engine._root_paths = lambda: ()
    engine._util_paths = lambda: {}

    dispatcher = engine._make_dispatcher("investigator", "session-2", deadline=123.0)
    assert isinstance(dispatcher, ToolDispatcher)


# ---------------------------------------------------------------------------
# _run_entry_agent
# ---------------------------------------------------------------------------


def _entry_agent_engine(*, gateway: _FakeGateway | None = None) -> WorkflowEngine:
    engine = _base_engine(gateway=gateway)
    engine._registry = SimpleNamespace(
        get=lambda name, autonomous=False: SimpleNamespace(
            name=name, capability="medium", tools=frozenset(), system_prompt="sys"
        )
    )
    engine._session = SessionState(session_id="s1")
    engine._session.effective_autonomous = False
    engine._main_messages = []
    engine._cycle_streak = False
    # _make_cyclic_thinking_handler (doc/STUCK_DETECTION.md §2.7) reads
    # settings/routing.residence eagerly at construction time (unlike
    # _make_stall_handler's lazy check); _run_agent_turn itself is faked out
    # below, so no test here actually exercises the cyclic-thinking path.
    engine._get_settings = lambda: {
        "stuck_detection": {
            "active": "local_only",
            "scope": "top_level",
            "auto_unstuck_interactive": False,
        }
    }

    async def _resolve_plugin(capability, force_model_key=None):
        return (SimpleNamespace(name="fake"), "model-x", SimpleNamespace(residence="local"))

    engine._resolve_plugin = _resolve_plugin
    engine._resolve_model_key = lambda capability: f"key-{capability}"

    dispatcher = SimpleNamespace(dispatch=None, stop_requested=False)

    engine._make_dispatcher = lambda agent_name, session_id, deadline=None: dispatcher

    async def _run_agent_turn(**kwargs):
        return (engine._main_messages, [])

    engine._run_agent_turn = _run_agent_turn
    return engine


async def test_run_entry_agent_persists_prompt_and_runs_turn() -> None:
    engine = _entry_agent_engine()

    await engine._run_entry_agent("guide", "hello there")

    assert engine._transient.appended[0][0] == "user"
    assert engine._session.phase == "awaiting_user"
    assert engine._session.agent is None
    assert engine._emitters.started == ["guide"]
    assert engine._emitters.finished == ["guide"]
    assert engine._compactor.auto_compact_calls == 1
    assert engine._compactor.noted == ["key-medium"]


async def test_run_entry_agent_phase_done_is_not_overridden() -> None:
    engine = _entry_agent_engine()

    async def _run_agent_turn(**kwargs):
        engine._session.phase = "done"
        return (engine._main_messages, [])

    engine._run_agent_turn = _run_agent_turn

    await engine._run_entry_agent("guide", "wrap up")

    assert engine._session.phase == "done"


async def test_run_entry_agent_with_attachments_sends_user_attachments_event(
    tmp_path: Path,
) -> None:
    engine = _entry_agent_engine()
    src = tmp_path / "note.txt"
    src.write_text("content")

    await engine._run_entry_agent("guide", "check this", [str(src)])

    attach_events = [e for e in engine._sink.sent if e.payload.get("type") == "user.attachments"]
    assert len(attach_events) == 1
    assert attach_events[0].payload["attachments"][0]["name"] == "note.txt"


async def test_run_entry_agent_attachment_errors_are_emitted() -> None:
    engine = _entry_agent_engine()

    await engine._run_entry_agent("guide", "check this", ["/nonexistent/path.txt"])

    assert len(engine._emitters.errors) == 1


async def test_run_entry_agent_blank_text_and_no_attachments_skips_message() -> None:
    engine = _entry_agent_engine()

    await engine._run_entry_agent("guide", "")

    assert engine._transient.appended == []

"""Unit tests for ``kodo.runtime._engine._compaction`` (in-place context
compaction).

``ContextCompactor`` is a self-contained collaborator (see the
``_engine/__init__.py`` module docstring) that reaches back into the engine
only through the narrow :class:`CompactorHost` protocol, so it is exercised
here with a small fake host rather than a real ``WorkflowEngine``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kodo.llms import Message
from kodo.runtime._engine import _compaction
from kodo.runtime._engine._compaction import (
    ContextCompactor,
    compaction_context_message,
    estimate_tokens,
    render_transcript,
)
from kodo.runtime._engine._events import EngineEmitters
from kodo.runtime._session import SessionState

# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def test_compaction_context_message_wraps_summary() -> None:
    msg = compaction_context_message("the gist of it")

    assert msg.role == "user"
    assert "the gist of it" in msg.content
    assert "compacted" in msg.content


def test_estimate_tokens_counts_string_content() -> None:
    messages = [Message(role="user", content="a" * 40)]
    assert estimate_tokens(messages) == 10


def test_estimate_tokens_counts_structured_content_as_json() -> None:
    messages = [Message(role="assistant", content=[{"type": "text", "text": "hi"}])]
    assert estimate_tokens(messages) >= 1


def test_estimate_tokens_never_returns_zero() -> None:
    assert estimate_tokens([Message(role="user", content="")]) == 1


def test_render_transcript_string_content() -> None:
    messages = [Message(role="user", content="hello there")]
    text = render_transcript(messages)
    assert "## USER" in text
    assert "hello there" in text


def test_render_transcript_strips_callouts_from_assistant_text_only() -> None:
    messages = [
        Message(role="user", content="<kodo_crit>ignore</kodo_crit> please help"),
        Message(role="assistant", content="<kodo_crit>hidden</kodo_crit> visible reply"),
    ]
    text = render_transcript(messages)
    # User content is passed through verbatim (callouts are assistant-only).
    assert "<kodo_crit>ignore</kodo_crit> please help" in text
    # Assistant callouts are stripped.
    assert "hidden" not in text
    assert "visible reply" in text


def test_render_transcript_renders_block_types() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "pondering"},
                {"type": "text", "text": "here's my answer"},
                {"type": "tool_use", "name": "run_command", "input": {"command": "ls"}},
            ],
        ),
        Message(
            role="user",
            content=[{"type": "tool_result", "content": "listing output"}],
        ),
    ]
    text = render_transcript(messages)
    assert "[thinking] pondering" in text
    assert "here's my answer" in text
    assert '[tool_use run_command] {"command": "ls"}' in text
    assert "[tool_result] listing output" in text


def test_render_transcript_skips_non_dict_blocks() -> None:
    messages = [Message(role="assistant", content=["not-a-dict"])]
    text = render_transcript(messages)
    assert text == "## ASSISTANT\n"


# ---------------------------------------------------------------------------
# ContextCompactor
# ---------------------------------------------------------------------------


class _FakeHost:
    def __init__(self, *, main_messages: list[Message], agent_available: bool = True) -> None:
        self._main_messages = main_messages
        self._agent_available_value = agent_available
        self.silent_turn_result: tuple[dict[str, object] | None, str] = (
            {"summary": "a tidy summary"},
            "",
        )
        self.silent_turn_error: Exception | None = None
        self.resolved_capabilities: list[str | None] = []

    def _agent_available(self, name: str) -> bool:
        return self._agent_available_value

    def _resolve_model_key(self, capability: str) -> str:
        return f"model-for-{capability}"

    def _entry_capability(self) -> str:
        return "medium"

    async def _resolve_plugin(self, capability: str, force_model_key: str | None = None):
        self.resolved_capabilities.append(force_model_key)
        return (SimpleNamespace(name="fake-plugin"), "model-x", SimpleNamespace())

    async def _run_silent_return_turn(self, routing, plugin, model_id, agent, messages):
        if self.silent_turn_error is not None:
            raise self.silent_turn_error
        return self.silent_turn_result


class _FakeRegistry:
    def get(self, name: str, autonomous: bool = False) -> SimpleNamespace:
        return SimpleNamespace(capability="medium", name=name)


class _FakeTransient:
    def __init__(self) -> None:
        self.markers: list[dict[str, object]] = []

    def append_marker(self, marker: dict[str, object]) -> None:
        self.markers.append(marker)


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


def _make_compactor(
    *, main_messages: list[Message] | None = None, agent_available: bool = True
) -> tuple[ContextCompactor, _FakeHost, _FakeTransient, _FakeSink]:
    host = _FakeHost(
        main_messages=main_messages
        if main_messages is not None
        else [Message(role="user", content="hi")],
        agent_available=agent_available,
    )
    transient = _FakeTransient()
    sink = _FakeSink()
    session = SessionState(session_id="s1", phase="awaiting_user")
    emitters = EngineEmitters(sink, session, lambda: {}, transient)
    compactor = ContextCompactor(
        host,  # type: ignore[arg-type]
        registry=_FakeRegistry(),  # type: ignore[arg-type]
        transient=transient,  # type: ignore[arg-type]
        sink=sink,  # type: ignore[arg-type]
        session=session,
        emitters=emitters,
    )
    return compactor, host, transient, sink


@pytest.fixture(autouse=True)
def _fixed_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the context window so tests don't depend on the real registry/user dir."""
    monkeypatch.setattr(_compaction, "get_context_window", lambda model_key, kodo_dir: 1000)


def test_context_tokens_property_getter_setter() -> None:
    compactor, _host, _t, _s = _make_compactor()
    assert compactor.context_tokens == 0
    compactor.context_tokens = 42
    assert compactor.context_tokens == 42


def test_note_active_model_records_key() -> None:
    compactor, _host, _t, _s = _make_compactor()
    compactor.note_active_model("claude-x")
    assert compactor._active_model_key == "claude-x"


def test_context_limit_resolves_via_host_and_registry() -> None:
    compactor, _host, _t, _s = _make_compactor()
    assert compactor.context_limit() == 1000


def test_can_compact_true_when_all_conditions_met() -> None:
    compactor, _host, _t, _s = _make_compactor()
    compactor.context_tokens = 10
    assert compactor.can_compact() is True


@pytest.mark.parametrize(
    ("phase", "compacting", "tokens", "messages", "agent_available"),
    [
        ("running", False, 10, [Message(role="user", content="x")], True),
        ("awaiting_user", True, 10, [Message(role="user", content="x")], True),
        ("awaiting_user", False, 0, [Message(role="user", content="x")], True),
        ("awaiting_user", False, 10, [], True),
        ("awaiting_user", False, 10, [Message(role="user", content="x")], False),
    ],
)
def test_can_compact_false_cases(phase, compacting, tokens, messages, agent_available) -> None:
    compactor, host, _t, _s = _make_compactor(
        main_messages=messages, agent_available=agent_available
    )
    compactor._session.phase = phase
    compactor._compacting = compacting
    compactor.context_tokens = tokens
    assert compactor.can_compact() is False


def test_context_stats_payload_shape() -> None:
    compactor, _host, _t, _s = _make_compactor()
    compactor.context_tokens = 500
    payload = compactor.context_stats_payload()
    assert payload == {
        "current_tokens": 500,
        "limit_tokens": 1000,
        "percent": 50.0,
        "can_compact": True,
    }


@pytest.mark.asyncio
async def test_maybe_auto_compact_noop_below_threshold() -> None:
    compactor, host, transient, _s = _make_compactor()
    compactor.context_tokens = 100  # well below 90% of 1000

    await compactor.maybe_auto_compact()

    assert transient.markers == []


@pytest.mark.asyncio
async def test_maybe_auto_compact_noop_when_already_compacting() -> None:
    compactor, host, transient, _s = _make_compactor()
    compactor._compacting = True
    compactor.context_tokens = 999

    await compactor.maybe_auto_compact()

    assert transient.markers == []


@pytest.mark.asyncio
async def test_maybe_auto_compact_runs_above_threshold() -> None:
    compactor, host, transient, sink = _make_compactor()
    compactor.context_tokens = 950  # >= 90% of 1000

    await compactor.maybe_auto_compact()

    assert len(transient.markers) == 1
    assert transient.markers[0]["reason"] == "auto"
    assert compactor._host._main_messages == [compaction_context_message("a tidy summary")]


@pytest.mark.asyncio
async def test_run_manual_compaction_ignored_when_not_compactable() -> None:
    compactor, host, transient, _s = _make_compactor(main_messages=[])

    await compactor.run_manual_compaction()

    assert transient.markers == []


@pytest.mark.asyncio
async def test_run_manual_compaction_runs_when_compactable() -> None:
    compactor, host, transient, _s = _make_compactor()
    compactor.context_tokens = 10

    await compactor.run_manual_compaction()

    assert len(transient.markers) == 1
    assert transient.markers[0]["reason"] == "manual"


@pytest.mark.asyncio
async def test_run_compaction_noop_when_no_main_messages() -> None:
    compactor, host, transient, sink = _make_compactor(main_messages=[])

    await compactor._run_compaction("manual")

    assert transient.markers == []
    assert sink.sent == []


@pytest.mark.asyncio
async def test_run_compaction_noop_when_agent_unavailable() -> None:
    compactor, host, transient, sink = _make_compactor(agent_available=False)

    await compactor._run_compaction("manual")

    assert transient.markers == []


@pytest.mark.asyncio
async def test_run_compaction_handles_summary_generation_exception() -> None:
    compactor, host, transient, sink = _make_compactor()
    host.silent_turn_error = RuntimeError("llm blew up")
    compactor.context_tokens = 100

    await compactor._run_compaction("manual")

    assert compactor._compacting is False
    assert transient.markers == []
    # Context gauge is refreshed via emit_context_stats even on failure.
    assert any(env.payload.get("type") == "context.stats" for env in sink.sent)


@pytest.mark.asyncio
async def test_run_compaction_noop_when_summary_empty() -> None:
    compactor, host, transient, sink = _make_compactor()
    host.silent_turn_result = (None, "   ")

    await compactor._run_compaction("manual")

    assert transient.markers == []


@pytest.mark.asyncio
async def test_run_compaction_success_resets_context_and_emits_event() -> None:
    compactor, host, transient, sink = _make_compactor(
        main_messages=[Message(role="user", content="long transcript" * 10)]
    )
    compactor.context_tokens = 800

    await compactor._run_compaction("manual")

    assert transient.markers[0]["type"] == "compaction"
    assert transient.markers[0]["summary"] == "a tidy summary"
    assert transient.markers[0]["tokens_before"] == 800
    assert host._main_messages == [compaction_context_message("a tidy summary")]
    assert compactor.context_tokens == estimate_tokens(
        [compaction_context_message("a tidy summary")]
    )

    compacted_events = [e for e in sink.sent if e.payload.get("type") == "context.compacted"]
    assert len(compacted_events) == 1
    assert compacted_events[0].payload["summary"] == "a tidy summary"


@pytest.mark.asyncio
async def test_generate_compaction_summary_falls_back_to_text_when_no_result() -> None:
    compactor, host, _t, _s = _make_compactor()
    host.silent_turn_result = (None, "  plain text summary  ")

    summary = await compactor._generate_compaction_summary()

    assert summary == "plain text summary"


@pytest.mark.asyncio
async def test_generate_compaction_summary_prefers_result_summary_field() -> None:
    compactor, host, _t, _s = _make_compactor()
    host.silent_turn_result = ({"summary": "  structured summary  "}, "ignored fallback text")

    summary = await compactor._generate_compaction_summary()

    assert summary == "structured summary"


@pytest.mark.asyncio
async def test_generate_compaction_summary_passes_force_model_key() -> None:
    compactor, host, _t, _s = _make_compactor()

    await compactor._generate_compaction_summary(force_model_key="old-model")

    assert host.resolved_capabilities == ["old-model"]


@pytest.mark.asyncio
async def test_handle_config_changed_no_active_model_just_records_and_emits() -> None:
    compactor, host, _t, sink = _make_compactor()

    await compactor.handle_config_changed()

    assert compactor._active_model_key == "model-for-medium"
    assert any(env.payload.get("type") == "context.stats" for env in sink.sent)


@pytest.mark.asyncio
async def test_handle_config_changed_same_model_key_no_compaction() -> None:
    compactor, host, transient, _s = _make_compactor()
    compactor.note_active_model("model-for-medium")
    compactor.context_tokens = 999

    await compactor.handle_config_changed()

    assert transient.markers == []


@pytest.mark.asyncio
async def test_handle_config_changed_shrinking_window_compacts_with_old_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compactor, host, transient, _s = _make_compactor()
    compactor.note_active_model("old-model")
    compactor.context_tokens = 999  # bigger than the new (shrunk) limit below

    def _window(model_key: str, kodo_dir: object) -> int:
        return 500 if model_key == "model-for-medium" else 1000

    monkeypatch.setattr(_compaction, "get_context_window", _window)

    await compactor.handle_config_changed()

    assert len(transient.markers) == 1
    assert transient.markers[0]["reason"] == "model_switch"
    assert host.resolved_capabilities == ["old-model"]
    assert compactor._active_model_key == "model-for-medium"

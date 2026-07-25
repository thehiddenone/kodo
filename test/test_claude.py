"""Tests for ``kodo.llms.anthropic._claude`` -- the Claude LLM plugin.

Covers:
* :func:`_thinking_param` for adaptive vs. non-adaptive models.
* :class:`ClaudePlugin` properties (``name``, ``supported_models``).
* :meth:`ClaudePlugin.cancel` sets the cancel event.
* :meth:`ClaudePlugin.stream_query` delegates to the internal stream.
* :meth:`ClaudePlugin.__raw_stream` parses Anthropic SDK stream events.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from kodo.llms._interface import (
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    TurnEnd,
    Usage,
)
from kodo.llms.anthropic._claude import (
    _ADAPTIVE_THINKING_MODELS,
    ClaudePlugin,
    _thinking_param,
)

# ---------------------------------------------------------------------------
# _thinking_param -- pure
# ---------------------------------------------------------------------------


def test_thinking_param_adaptive_model() -> None:
    """Models in _ADAPTIVE_THINKING_MODELS get {\"type\": \"adaptive\"}."""
    for model in _ADAPTIVE_THINKING_MODELS:
        assert _thinking_param(model) == {"type": "adaptive"}


def test_thinking_param_non_adaptive_model() -> None:
    """Non-adaptive models get {\"type\": \"enabled\", \"budget_tokens\": ...}."""
    result = _thinking_param("claude-3-5-sonnet-20241022")
    assert result == {"type": "enabled", "budget_tokens": 4096}


def test_thinking_param_opus_4_7() -> None:
    """claude-opus-4-7 is adaptive (in the newer tier)."""
    result = _thinking_param("claude-opus-4-7")
    assert result == {"type": "adaptive"}


def test_thinking_param_opus_4_8() -> None:
    """claude-opus-4-8 is adaptive."""
    result = _thinking_param("claude-opus-4-8")
    assert result == {"type": "adaptive"}


def test_thinking_param_sonnet_5() -> None:
    """claude-sonnet-5 is adaptive."""
    result = _thinking_param("claude-sonnet-5")
    assert result == {"type": "adaptive"}


# ---------------------------------------------------------------------------
# ClaudePlugin -- properties
# ---------------------------------------------------------------------------


def test_claude_plugin_name() -> None:
    plugin = ClaudePlugin(api_key="test-key-123")
    assert plugin.name == "anthropic"


def test_claude_plugin_supported_models() -> None:
    plugin = ClaudePlugin(api_key="test-key-123")
    models = plugin.supported_models
    assert isinstance(models, list)
    assert len(models) >= 1
    assert all(isinstance(m, str) for m in models)


# ---------------------------------------------------------------------------
# ClaudePlugin -- cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_plugin_cancel_no_stream_id() -> None:
    """Cancelling a non-existent stream_id is a no-op (no error)."""
    plugin = ClaudePlugin(api_key="test-key-123")
    # Should not raise.
    await plugin.cancel("nonexistent-stream")


@pytest.mark.asyncio
async def test_claude_plugin_cancel_sets_event() -> None:
    """cancel() sets the asyncio.Event for the given stream_id."""
    plugin = ClaudePlugin(api_key="test-key-123")
    # Pre-populate the cancel_events dict.
    event = asyncio.Event()
    plugin._ClaudePlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-1")
    assert event.is_set()


@pytest.mark.asyncio
async def test_claude_plugin_cancel_unknown_stream_is_noop() -> None:
    """Cancelling a stream that was never started is harmless."""
    plugin = ClaudePlugin(api_key="test-key-123")
    # Pre-populate with one stream.
    event = asyncio.Event()
    plugin._ClaudePlugin__cancel_events["stream-1"] = event

    # Cancel a different stream.
    await plugin.cancel("stream-999")
    # stream-1's event should NOT be set.
    assert not event.is_set()


# ---------------------------------------------------------------------------
# ClaudePlugin -- stream_query (mocked internal)
# ---------------------------------------------------------------------------


class _FakeEvent:
    """Stand-in for a StreamEvent-like object."""

    def __init__(self, event_type: str, **kwargs: Any) -> None:
        self.type = event_type
        self.__dict__.update(kwargs)


@pytest.mark.asyncio
async def test_claude_plugin_stream_query_delegates_to_raw_stream() -> None:
    """stream_query() delegates to __stream_with_retry which calls __raw_stream."""
    plugin = ClaudePlugin(api_key="test-key-123")

    async def _fake_raw_stream(**kwargs: Any) -> Any:
        yield _FakeEvent("content_block_delta", delta=_FakeEvent("text_delta", text="hello"))
        yield _FakeEvent("message_stop")

    plugin._ClaudePlugin__raw_stream = _fake_raw_stream

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    # The events should come through (wrapped in our fake shape).
    assert len(events) >= 1


# ---------------------------------------------------------------------------
# ClaudePlugin -- supported_models list shape
# ---------------------------------------------------------------------------


def test_claude_supported_models_contains_expected_models() -> None:
    plugin = ClaudePlugin(api_key="test-key-123")
    models = plugin.supported_models
    # The plugin advertises these specific models.
    assert "claude-opus-4-7" in models
    assert "claude-sonnet-4-6" in models


@pytest.mark.asyncio
async def test_claude_stream_query_yields_from_inner() -> None:
    """stream_query delegates to __stream_with_retry which yields from __raw_stream."""
    plugin = ClaudePlugin(api_key="test-key-123")

    captured_args: dict[str, Any] = {}

    async def _fake_with_retry(**kwargs: Any) -> Any:
        captured_args.update(kwargs)

        # Yield a fake event.
        class _FakeEvent:
            type = "content_block_delta"

        yield _FakeEvent()

    plugin._ClaudePlugin__stream_with_retry = _fake_with_retry

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    assert len(events) >= 1
    assert captured_args["stream_id"] == "s1"
    assert captured_args["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_claude_cancel_sets_event() -> None:
    """cancel() sets the asyncio.Event for the given stream_id."""
    plugin = ClaudePlugin(api_key="test-key-123")

    event = asyncio.Event()
    plugin._ClaudePlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-1")
    # The event should be set.
    assert event.is_set()
    # Note: cancel() does NOT remove the event from the dict -- __stream_with_retry
    # does that in its finally block.


# ---------------------------------------------------------------------------
# __raw_stream -- parsing the Anthropic SDK stream
# ---------------------------------------------------------------------------


def _make_text_delta(text: str) -> Any:
    """Build a real RawContentBlockDeltaEvent with a TextDelta.

    __raw_stream dispatches on isinstance() against the real anthropic SDK
    types, so a generic MagicMock (which is not an instance of them) would
    silently fall through every branch -- these fixtures must construct the
    actual SDK objects to exercise the parsing logic.
    """
    from anthropic.types import RawContentBlockDeltaEvent, TextDelta

    delta = TextDelta(type="text_delta", text=text)
    return RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=delta)


def _make_input_json_delta(partial: str) -> Any:
    """Build a real RawContentBlockDeltaEvent with an InputJSONDelta."""
    from anthropic.types import InputJSONDelta, RawContentBlockDeltaEvent

    delta = InputJSONDelta(type="input_json_delta", partial_json=partial)
    return RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=delta)


def _make_thinking_delta(text: str) -> Any:
    """Build a real RawContentBlockDeltaEvent with a RawThinkingDelta."""
    from anthropic.types import RawContentBlockDeltaEvent
    from anthropic.types import ThinkingDelta as RawThinkingDelta

    delta = RawThinkingDelta(type="thinking_delta", thinking=text)
    return RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=delta)


def _make_signature_delta(signature: str) -> Any:
    """Build a real RawContentBlockDeltaEvent with a SignatureDelta."""
    from anthropic.types import RawContentBlockDeltaEvent, SignatureDelta

    delta = SignatureDelta(type="signature_delta", signature=signature)
    return RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=delta)


def _make_content_block_start(block_type: str, block_id: str, name: str | None = None) -> Any:
    """Build a real RawContentBlockStartEvent wrapping a ToolUseBlock."""
    from anthropic.types import RawContentBlockStartEvent, ToolUseBlock

    assert block_type == "tool_use", "only tool_use blocks are exercised by these tests"
    block = ToolUseBlock(type="tool_use", id=block_id, name=name or "", input={})
    return RawContentBlockStartEvent(type="content_block_start", index=0, content_block=block)


def _make_content_block_stop() -> Any:
    """Build a real RawContentBlockStopEvent."""
    from anthropic.types import RawContentBlockStopEvent

    return RawContentBlockStopEvent(type="content_block_stop", index=0)


def _make_final_message(
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_creation: int = 0,
    cache_read: int = 0,
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Build a fake final message from stream.get_final_message()."""
    raw_usage = MagicMock()
    raw_usage.input_tokens = input_tokens
    raw_usage.output_tokens = output_tokens
    raw_usage.cache_creation_input_tokens = cache_creation
    raw_usage.cache_read_input_tokens = cache_read
    final = MagicMock()
    final.usage = raw_usage
    final.stop_reason = stop_reason
    return final


class _FakeStreamCtx:
    """A proper async context manager that yields events and supports get_final_message."""

    def __init__(self, events: list, final: MagicMock | None = None) -> None:
        self._events = list(events)
        self._idx = 0
        self._final = final

    async def __aenter__(self) -> _FakeStreamCtx:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _FakeStreamCtx:
        return self

    async def __anext__(self) -> MagicMock:
        if self._idx < len(self._events):
            event = self._events[self._idx]
            self._idx += 1
            return event
        raise StopAsyncIteration

    async def get_final_message(self) -> MagicMock:
        if self._final is not None:
            return self._final
        return _make_final_message()


def _make_plugin() -> ClaudePlugin:
    """Create a ClaudePlugin with a real SDK client that we will mock."""
    return ClaudePlugin(api_key="test-key")


def _patch_client(
    plugin: ClaudePlugin, stream_events: list, final: MagicMock | None = None
) -> None:
    """Replace the plugin's internal client.stream with our fake context manager."""
    fake_stream = _FakeStreamCtx(stream_events, final)
    plugin._ClaudePlugin__client.messages.stream = MagicMock(return_value=fake_stream)  # type: ignore[method-assign]


def _not_cancelled() -> MagicMock:
    """A cancel_event mock that reports as not-set.

    MagicMock(spec=asyncio.Event).is_set() returns a truthy MagicMock by
    default, which would make __raw_stream's `if cancel_event.is_set():
    return` fire on the very first event -- is_set must be pinned to False.
    """
    event = MagicMock(spec=asyncio.Event)
    event.is_set.return_value = False
    return event


@pytest.mark.asyncio
async def test_claude_raw_stream_yields_text_tokens() -> None:
    """__raw_stream yields TokenDelta events from text deltas."""
    plugin = _make_plugin()
    text_events = [_make_text_delta("Hello"), _make_text_delta(" world")]
    stop_event = _make_content_block_stop()
    final = _make_final_message()

    _patch_client(plugin, text_events + [stop_event], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    token_events = [e for e in events if isinstance(e, TokenDelta)]
    assert len(token_events) == 2
    assert token_events[0].text == "Hello"
    assert token_events[1].text == " world"
    # Should end with TurnEnd.
    assert any(isinstance(e, TurnEnd) for e in events)


@pytest.mark.asyncio
async def test_claude_raw_stream_yields_thinking_and_signature() -> None:
    """__raw_stream yields ThinkingDelta and ThinkingSignature events."""
    plugin = _make_plugin()
    thinking_event = _make_thinking_delta("Let me reason about this")
    sig_event = _make_signature_delta("signature-value")
    stop_event = _make_content_block_stop()
    final = _make_final_message()

    _patch_client(plugin, [thinking_event, sig_event, stop_event], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    thinking_events = [e for e in events if isinstance(e, ThinkingDelta)]
    sig_events = [e for e in events if isinstance(e, ThinkingSignature)]
    assert len(thinking_events) == 1
    assert thinking_events[0].text == "Let me reason about this"
    assert len(sig_events) == 1
    assert sig_events[0].signature == "signature-value"


@pytest.mark.asyncio
async def test_claude_raw_stream_yields_tool_call() -> None:
    """__raw_stream yields ToolCallEvent after content_block_stop for a tool use block."""
    plugin = _make_plugin()
    start_event = _make_content_block_start("tool_use", "tool_123", name="read_file")
    json_delta = _make_input_json_delta('{"path": "/foo/bar.py"}')
    stop_event = _make_content_block_stop()
    final = _make_final_message()

    _patch_client(plugin, [start_event, json_delta, stop_event], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "read_file"
    assert tool_events[0].tool_use_id == "tool_123"
    assert tool_events[0].tool_input == {"path": "/foo/bar.py"}


@pytest.mark.asyncio
async def test_claude_raw_stream_yields_tool_call_with_json_error() -> None:
    """__raw_stream handles malformed JSON in tool input gracefully."""
    plugin = _make_plugin()
    start_event = _make_content_block_start("tool_use", "tool_456", name="write_file")
    json_delta = _make_input_json_delta("not valid json {")
    stop_event = _make_content_block_stop()
    final = _make_final_message()

    _patch_client(plugin, [start_event, json_delta, stop_event], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_input == {"_raw": "not valid json {"}


@pytest.mark.asyncio
async def test_claude_raw_stream_cancel_stops_early() -> None:
    """__raw_stream stops yielding when cancel_event is set."""
    plugin = _make_plugin()
    cancel_event = asyncio.Event()
    cancel_event.set()
    stop_event = _make_content_block_stop()
    final = _make_final_message()

    _patch_client(plugin, [_make_text_delta("partial"), stop_event], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=cancel_event,
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    # The stream should have stopped before yielding any content.
    token_events = [e for e in events if isinstance(e, TokenDelta)]
    assert len(token_events) == 0


@pytest.mark.asyncio
async def test_claude_raw_stream_usage_includes_cache_tokens() -> None:
    """__raw_stream populates cache_write_tokens and cache_read_tokens from the final message."""
    plugin = _make_plugin()
    stop_event = _make_content_block_stop()
    final = _make_final_message(
        input_tokens=100,
        output_tokens=50,
        cache_creation=30,
        cache_read=20,
    )

    _patch_client(plugin, [stop_event], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert len(turn_ends) == 1
    usage: Usage = turn_ends[0].usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_write_tokens == 30
    assert usage.cache_read_tokens == 20


@pytest.mark.asyncio
async def test_claude_raw_stream_multiple_tool_blocks() -> None:
    """__raw_stream handles multiple tool use blocks in sequence."""
    plugin = _make_plugin()
    start1 = _make_content_block_start("tool_use", "tool_1", name="tool_a")
    json1 = _make_input_json_delta('{"a": 1}')
    stop1 = _make_content_block_stop()
    start2 = _make_content_block_start("tool_use", "tool_2", name="tool_b")
    json2 = _make_input_json_delta('{"b": 2}')
    stop2 = _make_content_block_stop()
    final = _make_final_message()

    _patch_client(plugin, [start1, json1, stop1, start2, json2, stop2], final)

    events = []
    async for event in plugin._ClaudePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="claude-opus-4-7",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 2
    assert tool_events[0].tool_name == "tool_a"
    assert tool_events[1].tool_name == "tool_b"

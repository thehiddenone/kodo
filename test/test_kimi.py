"""Tests for ``kodo.llms.kimi._kimi`` -- the Kimi (Moonshot AI) LLM plugin.

Mirrors test_deepseek.py's shape (properties/cancel/stream_query-delegation
sections, Chat-Completions-shaped ``__raw_stream`` fixtures) since Kimi is
reached the same way Qwen/Gemini/DeepSeek are: ``client.chat.completions.create``,
not ``client.responses.stream`` -- see kodo/llms/kimi/_kimi.py's module
docstring. The one real difference from the DeepSeek tests: Kimi's two model
families use two different reasoning-config mechanisms (kimi-k3 takes a
top-level ``reasoning_effort`` kwarg, everything else takes an
``extra_body={"thinking": {"type": "enabled"}}`` toggle), covered explicitly
below; there is no per-tool-call signature capture/replay to test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import openai
import pytest

from kodo.llms._interface import (
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    TurnEnd,
    Usage,
)
from kodo.llms.kimi._kimi import (
    _DEFAULT_REASONING_EFFORT,
    _REASONING_EFFORT,
    KimiPlugin,
    _map_finish_reason,
    _reasoning_kwargs_for,
)

# ---------------------------------------------------------------------------
# _reasoning_kwargs_for -- pure
# ---------------------------------------------------------------------------


def test_reasoning_kwargs_k3_uses_top_level_reasoning_effort() -> None:
    assert _reasoning_kwargs_for("kimi-k3") == {"reasoning_effort": "max"}


def test_reasoning_kwargs_k3_does_not_nest_in_extra_body() -> None:
    result = _reasoning_kwargs_for("kimi-k3")
    assert "extra_body" not in result


def test_reasoning_kwargs_code_model_uses_extra_body_thinking_toggle() -> None:
    assert _reasoning_kwargs_for("kimi-k2.7-code") == {
        "extra_body": {"thinking": {"type": "enabled"}}
    }


def test_reasoning_kwargs_code_model_does_not_set_reasoning_effort() -> None:
    result = _reasoning_kwargs_for("kimi-k2.7-code")
    assert "reasoning_effort" not in result


def test_reasoning_kwargs_unknown_model_falls_back_to_extra_body_toggle() -> None:
    """An unregistered/future model id defaults to the boolean-toggle shape, not K3's."""
    result = _reasoning_kwargs_for("kimi-k4-nano")
    assert result == {"extra_body": {"thinking": {"type": "enabled"}}}


def test_reasoning_effort_table_only_covers_k3() -> None:
    """Only kimi-k3 uses the graded reasoning_effort mechanism -- see module docstring."""
    assert set(_REASONING_EFFORT) == {"kimi-k3"}
    assert _REASONING_EFFORT["kimi-k3"] == _DEFAULT_REASONING_EFFORT


# ---------------------------------------------------------------------------
# _map_finish_reason -- pure
# ---------------------------------------------------------------------------


def test_map_finish_reason_stop() -> None:
    assert _map_finish_reason("stop") == "end_turn"


def test_map_finish_reason_tool_calls() -> None:
    assert _map_finish_reason("tool_calls") == "tool_use"


def test_map_finish_reason_length() -> None:
    assert _map_finish_reason("length") == "max_tokens"


def test_map_finish_reason_unknown_passthrough() -> None:
    assert _map_finish_reason("content_filter") == "content_filter"


def test_map_finish_reason_none_is_end_turn() -> None:
    assert _map_finish_reason(None) == "end_turn"


# ---------------------------------------------------------------------------
# KimiPlugin -- properties
# ---------------------------------------------------------------------------


def test_kimi_plugin_name() -> None:
    plugin = KimiPlugin(api_key="test-key-123")
    assert plugin.name == "kimi"


def test_kimi_plugin_supported_models() -> None:
    plugin = KimiPlugin(api_key="test-key-123")
    models = plugin.supported_models
    assert isinstance(models, list)
    assert "kimi-k3" in models
    assert "kimi-k2.7-code" in models


# ---------------------------------------------------------------------------
# KimiPlugin -- cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kimi_plugin_cancel_no_stream_id() -> None:
    plugin = KimiPlugin(api_key="test-key-123")
    await plugin.cancel("nonexistent-stream")  # should not raise


@pytest.mark.asyncio
async def test_kimi_plugin_cancel_sets_event() -> None:
    plugin = KimiPlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._KimiPlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-1")
    assert event.is_set()


@pytest.mark.asyncio
async def test_kimi_plugin_cancel_unknown_stream_is_noop() -> None:
    plugin = KimiPlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._KimiPlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-999")
    assert not event.is_set()


# ---------------------------------------------------------------------------
# KimiPlugin -- stream_query (mocked internal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kimi_stream_query_yields_from_inner() -> None:
    """stream_query delegates to __stream_with_retry which yields from __raw_stream."""
    plugin = KimiPlugin(api_key="test-key-123")
    captured_args: dict[str, Any] = {}

    async def _fake_with_retry(**kwargs: Any) -> Any:
        captured_args.update(kwargs)
        yield TokenDelta(text="hi")

    plugin._KimiPlugin__stream_with_retry = _fake_with_retry

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="kimi-k2.7-code",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    assert len(events) == 1
    assert captured_args["stream_id"] == "s1"
    assert captured_args["model"] == "kimi-k2.7-code"


# ---------------------------------------------------------------------------
# __raw_stream -- mocked OpenAI client covering the streaming loop
# ---------------------------------------------------------------------------


def _make_chunk(
    content: str | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list | None = None,
    usage: object | None = None,
) -> MagicMock:
    """Build a fake OpenAI ChatCompletionChunk.

    Mirrors test_deepseek.py's helper of the same name (Kimi's OpenAI-
    compatible endpoint speaks the same Chat Completions wire shape).
    *reasoning_content*/*tool_calls* default to ``None`` explicitly -- a bare
    :class:`MagicMock` attribute is truthy, which would trick the plugin's
    ``if reasoning_content:`` guard into treating the placeholder as real
    content.
    """
    choice = MagicMock()
    choice.finish_reason = finish_reason
    delta = MagicMock()
    delta.content = content
    delta.reasoning_content = reasoning_content
    delta.tool_calls = tool_calls
    choice.delta = delta
    chunk = MagicMock()
    chunk.choices = (
        [choice] if (content or reasoning_content or tool_calls or finish_reason) else []
    )
    chunk.usage = usage
    return chunk


class _FakeAsyncStream:
    """A real async-iterable that yields the given chunks in order.

    Mirrors test_deepseek.py's helper of the same name. An optional *on_each*
    callback is invoked after each chunk is yielded, enabling tests to
    mutate state (e.g. set a cancel event) mid-stream.
    """

    def __init__(self, chunks: list, on_each: Callable[[MagicMock], None] | None = None) -> None:
        self._chunks = list(chunks)
        self._on_each = on_each

    def __aiter__(self) -> _FakeAsyncStream:
        return self

    async def __anext__(self) -> MagicMock:
        if not self._chunks:
            raise StopAsyncIteration
        result = self._chunks.pop(0)
        if self._on_each is not None:
            self._on_each(result)
        return result


def _not_cancelled() -> MagicMock:
    event = MagicMock(spec=asyncio.Event)
    event.is_set.return_value = False
    return event


def _make_plugin() -> KimiPlugin:
    plugin = KimiPlugin(api_key="test-key")
    plugin._KimiPlugin__client = MagicMock(spec=openai.AsyncOpenAI)
    return plugin


def _patch_client(
    plugin: KimiPlugin,
    chunks: list,
    on_each: Any = None,
    captured_kwargs: dict[str, Any] | None = None,
) -> None:
    async def _fake_create(**kwargs: Any) -> Any:
        if captured_kwargs is not None:
            captured_kwargs.update(kwargs)
        return _FakeAsyncStream(chunks, on_each=on_each)

    plugin._KimiPlugin__client.chat.completions.create = _fake_create


def _usage(prompt_tokens: int, completion_tokens: int, cached_tokens: int | None = 0) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    if cached_tokens is None:
        usage.prompt_tokens_details = None
    else:
        usage.prompt_tokens_details = MagicMock(cached_tokens=cached_tokens)
    return usage


@pytest.mark.asyncio
async def test_kimi_raw_stream_k3_sends_top_level_reasoning_effort() -> None:
    plugin = _make_plugin()
    captured: dict[str, Any] = {}
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(1, 1))],
        captured_kwargs=captured,
    )

    async for _ in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k3",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        pass

    assert captured["reasoning_effort"] == "max"
    assert "extra_body" not in captured
    assert captured["model"] == "kimi-k3"


@pytest.mark.asyncio
async def test_kimi_raw_stream_code_model_sends_extra_body_thinking_toggle() -> None:
    plugin = _make_plugin()
    captured: dict[str, Any] = {}
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(1, 1))],
        captured_kwargs=captured,
    )

    async for _ in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        pass

    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort" not in captured
    assert captured["model"] == "kimi-k2.7-code"


@pytest.mark.asyncio
async def test_kimi_raw_stream_token_deltas() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(content="Hello"),
            _make_chunk(content=" world"),
            _make_chunk(finish_reason="stop", usage=_usage(10, 5)),
        ],
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert token_texts == ["Hello", " world"]
    assert isinstance(events[-1], TurnEnd)


@pytest.mark.asyncio
async def test_kimi_raw_stream_reasoning_content_passthrough() -> None:
    """delta.reasoning_content becomes ThinkingDelta directly -- no tag parsing."""
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(reasoning_content="Let me think"),
            _make_chunk(reasoning_content=" about this"),
            _make_chunk(content="42", finish_reason="stop", usage=_usage(5, 3)),
        ],
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k3",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    thinking_texts = [e.text for e in events if isinstance(e, ThinkingDelta)]
    assert thinking_texts == ["Let me think", " about this"]


@pytest.mark.asyncio
async def test_kimi_raw_stream_tool_calls() -> None:
    plugin = _make_plugin()
    fake_tool_call = MagicMock()
    fake_tool_call.index = 0
    fake_tool_call.id = "call_abc"
    fake_tool_call.function = MagicMock()
    fake_tool_call.function.name = "read_file"
    fake_tool_call.function.arguments = '{"path": "/foo"}'

    _patch_client(
        plugin,
        [
            _make_chunk(tool_calls=[fake_tool_call]),
            _make_chunk(finish_reason="tool_calls", usage=_usage(10, 5)),
        ],
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "read_file"
    assert tool_events[0].tool_use_id == "call_abc"
    assert tool_events[0].tool_input == {"path": "/foo"}
    assert tool_events[0].thought_signature is None

    arg_events = [e for e in events if isinstance(e, ToolCallArgDelta)]
    assert len(arg_events) == 1

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_kimi_raw_stream_tool_call_malformed_json() -> None:
    plugin = _make_plugin()
    fake_tool_call = MagicMock()
    fake_tool_call.index = 0
    fake_tool_call.id = "call_1"
    fake_tool_call.function = MagicMock()
    fake_tool_call.function.name = "write_file"
    fake_tool_call.function.arguments = "not valid json {"

    _patch_client(
        plugin,
        [
            _make_chunk(tool_calls=[fake_tool_call]),
            _make_chunk(finish_reason="tool_calls", usage=_usage(1, 1)),
        ],
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_input == {"_raw": "not valid json {"}


@pytest.mark.asyncio
async def test_kimi_raw_stream_cancel_stops_stream() -> None:
    plugin = _make_plugin()
    cancel_event = asyncio.Event()

    _cancel_state = {"fetches": 0}

    def _set_cancel_on_second_fetch(chunk: MagicMock) -> None:
        _cancel_state["fetches"] += 1
        if _cancel_state["fetches"] >= 2:
            cancel_event.set()

    _patch_client(
        plugin,
        [_make_chunk(content="first part"), _make_chunk(content="second part")],
        on_each=_set_cancel_on_second_fetch,
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=cancel_event,
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert token_texts == ["first part"]
    assert not any(isinstance(e, TurnEnd) for e in events)


@pytest.mark.asyncio
async def test_kimi_raw_stream_usage_includes_cache_read_tokens() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(100, 50, cached_tokens=20))],
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    usage: Usage = turn_ends[0].usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_write_tokens == 0
    assert usage.cache_read_tokens == 20
    assert usage.model == "kimi-k2.7-code"


@pytest.mark.asyncio
async def test_kimi_raw_stream_usage_missing_cache_details_defaults_to_zero() -> None:
    """A usage payload with no prompt_tokens_details degrades to cache_read_tokens=0."""
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(10, 5, cached_tokens=None))],
    )

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.cache_read_tokens == 0


@pytest.mark.asyncio
async def test_kimi_raw_stream_stop_reason_max_tokens() -> None:
    plugin = _make_plugin()
    _patch_client(plugin, [_make_chunk(finish_reason="length", usage=_usage(10, 5))])

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_kimi_raw_stream_no_tool_call_is_end_turn() -> None:
    plugin = _make_plugin()
    _patch_client(plugin, [_make_chunk(content="hi", finish_reason="stop", usage=_usage(10, 5))])

    events = []
    async for event in plugin._KimiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="kimi-k2.7-code",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "end_turn"

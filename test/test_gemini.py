"""Tests for ``kodo.llms.google._gemini`` -- the Google Gemini LLM plugin.

Mirrors test_gpt.py's shape (properties/cancel/stream_query-delegation
sections), but the ``__raw_stream`` fixtures are Chat-Completions-shaped
(``_make_chunk``/``_FakeAsyncStream``/``_not_cancelled``, mirroring
test_llama_pure.py's helpers) rather than Responses-API-shaped, since Gemini
is reached via ``client.chat.completions.create``, not
``client.responses.stream`` -- see kodo/llms/google/_gemini.py's module
docstring.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import openai
import pytest

from kodo.llms._cloud_thinking import cloud_thinking_default_tier, cloud_thinking_tiers
from kodo.llms._interface import (
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    TurnEnd,
    Usage,
)
from kodo.llms.google._gemini import (
    _DEFAULT_REASONING_EFFORT,
    _REASONING_EFFORTS,
    GeminiPlugin,
    _map_finish_reason,
    _reasoning_effort_for,
)

# ---------------------------------------------------------------------------
# _reasoning_effort_for -- pure
# ---------------------------------------------------------------------------


def test_reasoning_effort_forwards_every_valid_tier() -> None:
    """Gemini's thinking levels are the tier slugs -- forwarded verbatim."""
    for tier in sorted(_REASONING_EFFORTS):
        assert _reasoning_effort_for(tier) == tier


def test_reasoning_effort_none_falls_back_to_default() -> None:
    assert _reasoning_effort_for(None) == _DEFAULT_REASONING_EFFORT


def test_reasoning_effort_rejects_max_tier() -> None:
    """Gemini has no "max" level -- a stale client value must not be sent on."""
    assert _reasoning_effort_for("max") == _DEFAULT_REASONING_EFFORT


def test_reasoning_effort_accepts_exactly_the_registered_family_tiers() -> None:
    """Accepted set == the family catalog the server advertises (see test_gpt)."""
    assert set(cloud_thinking_tiers("google")) == _REASONING_EFFORTS
    assert cloud_thinking_default_tier("google") == _DEFAULT_REASONING_EFFORT


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
# GeminiPlugin -- properties
# ---------------------------------------------------------------------------


def test_gemini_plugin_name() -> None:
    plugin = GeminiPlugin(api_key="test-key-123")
    assert plugin.name == "google"


def test_gemini_plugin_supported_models() -> None:
    plugin = GeminiPlugin(api_key="test-key-123")
    models = plugin.supported_models
    assert isinstance(models, list)
    assert "gemini-3.6-flash" in models
    assert "gemini-3.5-flash-lite" in models


# ---------------------------------------------------------------------------
# GeminiPlugin -- cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_plugin_cancel_no_stream_id() -> None:
    plugin = GeminiPlugin(api_key="test-key-123")
    await plugin.cancel("nonexistent-stream")  # should not raise


@pytest.mark.asyncio
async def test_gemini_plugin_cancel_sets_event() -> None:
    plugin = GeminiPlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._GeminiPlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-1")
    assert event.is_set()


@pytest.mark.asyncio
async def test_gemini_plugin_cancel_unknown_stream_is_noop() -> None:
    plugin = GeminiPlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._GeminiPlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-999")
    assert not event.is_set()


# ---------------------------------------------------------------------------
# GeminiPlugin -- stream_query (mocked internal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_stream_query_yields_from_inner() -> None:
    """stream_query delegates to __stream_with_retry which yields from __raw_stream."""
    plugin = GeminiPlugin(api_key="test-key-123")
    captured_args: dict[str, Any] = {}

    async def _fake_with_retry(**kwargs: Any) -> Any:
        captured_args.update(kwargs)
        yield TokenDelta(text="hi")

    plugin._GeminiPlugin__stream_with_retry = _fake_with_retry

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="gemini-3.6-flash",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    assert len(events) == 1
    assert captured_args["stream_id"] == "s1"
    assert captured_args["model"] == "gemini-3.6-flash"


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

    Mirrors test_llama_pure.py's helper of the same name (Gemini's OpenAI-
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

    Mirrors test_llama_pure.py's helper of the same name. An optional
    *on_each* callback is invoked after each chunk is yielded, enabling tests
    to mutate state (e.g. set a cancel event) mid-stream.
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


def _make_plugin() -> GeminiPlugin:
    plugin = GeminiPlugin(api_key="test-key")
    plugin._GeminiPlugin__client = MagicMock(spec=openai.AsyncOpenAI)
    return plugin


def _patch_client(plugin: GeminiPlugin, chunks: list, on_each: Any = None) -> None:
    async def _fake_create(**kwargs: Any) -> Any:
        return _FakeAsyncStream(chunks, on_each=on_each)

    plugin._GeminiPlugin__client.chat.completions.create = _fake_create


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
async def test_gemini_raw_stream_token_deltas() -> None:
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
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert token_texts == ["Hello", " world"]
    assert isinstance(events[-1], TurnEnd)


@pytest.mark.asyncio
async def test_gemini_raw_stream_reasoning_content_passthrough() -> None:
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
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    thinking_texts = [e.text for e in events if isinstance(e, ThinkingDelta)]
    assert thinking_texts == ["Let me think", " about this"]


@pytest.mark.asyncio
async def test_gemini_raw_stream_tool_calls() -> None:
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
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
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
async def test_gemini_raw_stream_tool_call_captures_thought_signature() -> None:
    """Gemini's per-call `extra_content.google.thought_signature` reaches ToolCallEvent."""
    plugin = _make_plugin()
    fake_tool_call = MagicMock()
    fake_tool_call.index = 0
    fake_tool_call.id = "call_abc"
    fake_tool_call.function = MagicMock()
    fake_tool_call.function.name = "read_file"
    fake_tool_call.function.arguments = '{"path": "/foo"}'
    fake_tool_call.extra_content = {"google": {"thought_signature": "sig-xyz"}}

    _patch_client(
        plugin,
        [
            _make_chunk(tool_calls=[fake_tool_call]),
            _make_chunk(finish_reason="tool_calls", usage=_usage(10, 5)),
        ],
    )

    events = []
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].thought_signature == "sig-xyz"


@pytest.mark.asyncio
async def test_gemini_raw_stream_tool_call_ignores_non_dict_extra_content() -> None:
    """A MagicMock (or any non-dict) extra_content is treated as absent, not a crash."""
    plugin = _make_plugin()
    fake_tool_call = MagicMock()
    fake_tool_call.index = 0
    fake_tool_call.id = "call_abc"
    fake_tool_call.function = MagicMock()
    fake_tool_call.function.name = "read_file"
    fake_tool_call.function.arguments = "{}"
    # No extra_content set -- MagicMock auto-vivifies attribute access, so
    # getattr(tc, "extra_content", None) returns a MagicMock, not None; the
    # isinstance(..., dict) guard must reject that rather than crash.

    _patch_client(
        plugin,
        [
            _make_chunk(tool_calls=[fake_tool_call]),
            _make_chunk(finish_reason="tool_calls", usage=_usage(1, 1)),
        ],
    )

    events = []
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert tool_events[0].thought_signature is None


@pytest.mark.asyncio
async def test_gemini_raw_stream_tool_call_malformed_json() -> None:
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
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_input == {"_raw": "not valid json {"}


@pytest.mark.asyncio
async def test_gemini_raw_stream_cancel_stops_stream() -> None:
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
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=cancel_event,
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert token_texts == ["first part"]
    assert not any(isinstance(e, TurnEnd) for e in events)


@pytest.mark.asyncio
async def test_gemini_raw_stream_usage_includes_cache_read_tokens() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(100, 50, cached_tokens=20))],
    )

    events = []
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    usage: Usage = turn_ends[0].usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_write_tokens == 0
    assert usage.cache_read_tokens == 20
    assert usage.model == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_gemini_raw_stream_usage_missing_cache_details_defaults_to_zero() -> None:
    """A usage payload with no prompt_tokens_details degrades to cache_read_tokens=0."""
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(10, 5, cached_tokens=None))],
    )

    events = []
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.cache_read_tokens == 0


@pytest.mark.asyncio
async def test_gemini_raw_stream_stop_reason_max_tokens() -> None:
    plugin = _make_plugin()
    _patch_client(plugin, [_make_chunk(finish_reason="length", usage=_usage(10, 5))])

    events = []
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_gemini_raw_stream_no_tool_call_is_end_turn() -> None:
    plugin = _make_plugin()
    _patch_client(plugin, [_make_chunk(content="hi", finish_reason="stop", usage=_usage(10, 5))])

    events = []
    async for event in plugin._GeminiPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="gemini-3.6-flash",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "end_turn"

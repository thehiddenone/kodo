"""Tests for ``kodo.llms.openrouter._openrouter`` -- the OpenRouter LLM plugin.

Mirrors test_kimi.py's shape (properties/cancel/stream_query-delegation
sections, Chat-Completions-shaped ``__raw_stream`` fixtures) since OpenRouter
is reached the same way Qwen/Gemini/DeepSeek/Kimi are:
``client.chat.completions.create``. The real differences from the Kimi
tests, covered explicitly below: reasoning effort comes from a
``thinking_level`` parameter (not a per-model table), reasoning text arrives
on ``delta.reasoning_details`` (a list of typed objects) rather than a flat
``delta.reasoning_content`` string, the served model is captured off each
chunk's own ``model`` field, and cost is read off ``usage.cost`` into
``Usage.provider_reported_cost`` rather than computed from a pricing table.
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
from kodo.llms.openrouter._openrouter import (
    OpenRouterPlugin,
    _map_finish_reason,
)

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
# OpenRouterPlugin -- properties
# ---------------------------------------------------------------------------


def test_openrouter_plugin_name() -> None:
    plugin = OpenRouterPlugin(api_key="test-key-123")
    assert plugin.name == "openrouter"


def test_openrouter_plugin_supported_models_includes_auto() -> None:
    plugin = OpenRouterPlugin(api_key="test-key-123")
    assert "openrouter/auto" in plugin.supported_models


# ---------------------------------------------------------------------------
# OpenRouterPlugin -- cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_plugin_cancel_no_stream_id() -> None:
    plugin = OpenRouterPlugin(api_key="test-key-123")
    await plugin.cancel("nonexistent-stream")  # should not raise


@pytest.mark.asyncio
async def test_openrouter_plugin_cancel_sets_event() -> None:
    plugin = OpenRouterPlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._OpenRouterPlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-1")
    assert event.is_set()


@pytest.mark.asyncio
async def test_openrouter_plugin_cancel_unknown_stream_is_noop() -> None:
    plugin = OpenRouterPlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._OpenRouterPlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-999")
    assert not event.is_set()


# ---------------------------------------------------------------------------
# OpenRouterPlugin -- stream_query (mocked internal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_stream_query_yields_from_inner() -> None:
    """stream_query delegates to __stream_with_retry which yields from __raw_stream."""
    plugin = OpenRouterPlugin(api_key="test-key-123")
    captured_args: dict[str, Any] = {}

    async def _fake_with_retry(**kwargs: Any) -> Any:
        captured_args.update(kwargs)
        yield TokenDelta(text="hi")

    plugin._OpenRouterPlugin__stream_with_retry = _fake_with_retry

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="openrouter/auto",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level="high",
    ):
        events.append(event)

    assert len(events) == 1
    assert captured_args["stream_id"] == "s1"
    assert captured_args["model"] == "openrouter/auto"
    assert captured_args["thinking_level"] == "high"


# ---------------------------------------------------------------------------
# __raw_stream -- mocked OpenAI client covering the streaming loop
# ---------------------------------------------------------------------------


def _make_chunk(
    content: str | None = None,
    reasoning_details: list | None = None,
    reasoning: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list | None = None,
    usage: object | None = None,
    model: str | None = "openrouter/auto",
) -> MagicMock:
    """Build a fake OpenAI ChatCompletionChunk.

    Mirrors test_kimi.py's helper of the same name. Every optional field
    defaults to ``None`` explicitly -- a bare :class:`MagicMock` attribute is
    truthy, which would trick a plugin ``if x:`` guard into treating the
    placeholder as real content.
    """
    choice = MagicMock()
    choice.finish_reason = finish_reason
    delta = MagicMock()
    delta.content = content
    delta.reasoning_details = reasoning_details
    delta.reasoning = reasoning
    delta.tool_calls = tool_calls
    choice.delta = delta
    chunk = MagicMock()
    has_content = content or reasoning_details or reasoning or tool_calls or finish_reason
    chunk.choices = [choice] if has_content else []
    chunk.usage = usage
    chunk.model = model
    return chunk


class _FakeAsyncStream:
    """A real async-iterable that yields the given chunks in order.

    Mirrors test_kimi.py's helper of the same name. An optional *on_each*
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


def _make_plugin() -> OpenRouterPlugin:
    plugin = OpenRouterPlugin(api_key="test-key")
    plugin._OpenRouterPlugin__client = MagicMock(spec=openai.AsyncOpenAI)
    return plugin


def _patch_client(
    plugin: OpenRouterPlugin,
    chunks: list,
    on_each: Any = None,
    captured_kwargs: dict[str, Any] | None = None,
) -> None:
    async def _fake_create(**kwargs: Any) -> Any:
        if captured_kwargs is not None:
            captured_kwargs.update(kwargs)
        return _FakeAsyncStream(chunks, on_each=on_each)

    plugin._OpenRouterPlugin__client.chat.completions.create = _fake_create


def _usage(
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int | None = 0,
    cost: float | None = None,
) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    if cached_tokens is None:
        usage.prompt_tokens_details = None
    else:
        usage.prompt_tokens_details = MagicMock(cached_tokens=cached_tokens)
    usage.cost = cost
    return usage


async def _run_raw_stream(
    plugin: OpenRouterPlugin, model: str, thinking_level: str | None = None
) -> list:
    events = []
    async for event in plugin._OpenRouterPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model=model,
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=thinking_level,
    ):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# __raw_stream -- thinking_level -> reasoning.effort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_sends_reasoning_effort_when_thinking_level_set() -> None:
    plugin = _make_plugin()
    captured: dict[str, Any] = {}
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(1, 1))],
        captured_kwargs=captured,
    )

    await _run_raw_stream(plugin, "openrouter/auto", thinking_level="high")

    assert captured["extra_body"] == {"reasoning": {"effort": "high"}}
    assert captured["model"] == "openrouter/auto"


@pytest.mark.asyncio
async def test_raw_stream_omits_reasoning_when_thinking_level_none() -> None:
    plugin = _make_plugin()
    captured: dict[str, Any] = {}
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(1, 1))],
        captured_kwargs=captured,
    )

    await _run_raw_stream(plugin, "openrouter/auto", thinking_level=None)

    assert captured["extra_body"] == {}


@pytest.mark.asyncio
async def test_raw_stream_ignores_unrecognized_thinking_level() -> None:
    """Only kodo's own low/medium/high/max vocabulary is ever forwarded."""
    plugin = _make_plugin()
    captured: dict[str, Any] = {}
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(1, 1))],
        captured_kwargs=captured,
    )

    await _run_raw_stream(plugin, "openrouter/auto", thinking_level="xhigh")

    assert captured["extra_body"] == {}


# ---------------------------------------------------------------------------
# __raw_stream -- token deltas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_token_deltas() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(content="Hello"),
            _make_chunk(content=" world"),
            _make_chunk(finish_reason="stop", usage=_usage(10, 5)),
        ],
    )

    events = await _run_raw_stream(plugin, "openrouter/auto")

    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert token_texts == ["Hello", " world"]
    assert isinstance(events[-1], TurnEnd)


# ---------------------------------------------------------------------------
# __raw_stream -- reasoning_details parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_reasoning_details_becomes_thinking_delta() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(reasoning_details=[{"type": "reasoning.text", "text": "Let me think"}]),
            _make_chunk(reasoning_details=[{"type": "reasoning.text", "text": " about this"}]),
            _make_chunk(content="42", finish_reason="stop", usage=_usage(5, 3)),
        ],
    )

    events = await _run_raw_stream(plugin, "openrouter/auto")

    thinking_texts = [e.text for e in events if isinstance(e, ThinkingDelta)]
    assert thinking_texts == ["Let me think", " about this"]


@pytest.mark.asyncio
async def test_raw_stream_reasoning_details_skips_non_text_entries() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(
                reasoning_details=[
                    {"type": "reasoning.summary", "text": "should be ignored"},
                    {"type": "reasoning.text", "text": "kept"},
                ]
            ),
            _make_chunk(content="ok", finish_reason="stop", usage=_usage(1, 1)),
        ],
    )

    events = await _run_raw_stream(plugin, "openrouter/auto")

    thinking_texts = [e.text for e in events if isinstance(e, ThinkingDelta)]
    assert thinking_texts == ["kept"]


@pytest.mark.asyncio
async def test_raw_stream_falls_back_to_flat_reasoning_string() -> None:
    """A model proxied through OpenRouter's compatibility shim may emit the simpler shape."""
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(reasoning="flat reasoning text"),
            _make_chunk(content="ok", finish_reason="stop", usage=_usage(1, 1)),
        ],
    )

    events = await _run_raw_stream(plugin, "openrouter/auto")

    thinking_texts = [e.text for e in events if isinstance(e, ThinkingDelta)]
    assert thinking_texts == ["flat reasoning text"]


# ---------------------------------------------------------------------------
# __raw_stream -- served model capture (the whole point of it for "auto")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_usage_model_reflects_actual_served_model() -> None:
    """Requesting "openrouter/auto" but the response reports the real upstream model."""
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [
            _make_chunk(content="hi", model="anthropic/claude-sonnet-4"),
            _make_chunk(
                finish_reason="stop", usage=_usage(10, 5), model="anthropic/claude-sonnet-4"
            ),
        ],
    )

    events = await _run_raw_stream(plugin, "openrouter/auto")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.model == "anthropic/claude-sonnet-4"


@pytest.mark.asyncio
async def test_raw_stream_usage_model_falls_back_to_requested_model_if_chunk_has_none() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(1, 1), model=None)],
    )

    events = await _run_raw_stream(plugin, "openrouter/auto")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.model == "openrouter/auto"


# ---------------------------------------------------------------------------
# __raw_stream -- provider_reported_cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_captures_provider_reported_cost() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(100, 50, cost=0.00234))],
    )

    events = await _run_raw_stream(plugin, "anthropic/claude-sonnet-4")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    usage: Usage = turn_ends[0].usage
    assert usage.provider_reported_cost == 0.00234
    assert usage.usd_cost == 0.00234


@pytest.mark.asyncio
async def test_raw_stream_missing_cost_leaves_provider_reported_cost_none() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(100, 50, cost=None))],
    )

    events = await _run_raw_stream(plugin, "anthropic/claude-sonnet-4")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.provider_reported_cost is None


# ---------------------------------------------------------------------------
# __raw_stream -- tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_tool_calls() -> None:
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

    events = await _run_raw_stream(plugin, "openrouter/auto")

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "read_file"
    assert tool_events[0].tool_use_id == "call_abc"
    assert tool_events[0].tool_input == {"path": "/foo"}

    arg_events = [e for e in events if isinstance(e, ToolCallArgDelta)]
    assert len(arg_events) == 1

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_raw_stream_tool_call_malformed_json() -> None:
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

    events = await _run_raw_stream(plugin, "openrouter/auto")

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_input == {"_raw": "not valid json {"}


# ---------------------------------------------------------------------------
# __raw_stream -- cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_cancel_stops_stream() -> None:
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
    async for event in plugin._OpenRouterPlugin__raw_stream(
        cancel_event=cancel_event,
        model="openrouter/auto",
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


# ---------------------------------------------------------------------------
# __raw_stream -- usage / stop reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_stream_usage_includes_cache_read_tokens() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(100, 50, cached_tokens=20))],
    )

    events = await _run_raw_stream(plugin, "anthropic/claude-sonnet-4")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    usage: Usage = turn_ends[0].usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_write_tokens == 0
    assert usage.cache_read_tokens == 20


@pytest.mark.asyncio
async def test_raw_stream_usage_missing_cache_details_defaults_to_zero() -> None:
    plugin = _make_plugin()
    _patch_client(
        plugin,
        [_make_chunk(content="hi", finish_reason="stop", usage=_usage(10, 5, cached_tokens=None))],
    )

    events = await _run_raw_stream(plugin, "anthropic/claude-sonnet-4")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.cache_read_tokens == 0


@pytest.mark.asyncio
async def test_raw_stream_stop_reason_max_tokens() -> None:
    plugin = _make_plugin()
    _patch_client(plugin, [_make_chunk(finish_reason="length", usage=_usage(10, 5))])

    events = await _run_raw_stream(plugin, "anthropic/claude-sonnet-4")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_raw_stream_no_tool_call_is_end_turn() -> None:
    plugin = _make_plugin()
    _patch_client(plugin, [_make_chunk(content="hi", finish_reason="stop", usage=_usage(10, 5))])

    events = await _run_raw_stream(plugin, "anthropic/claude-sonnet-4")

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "end_turn"

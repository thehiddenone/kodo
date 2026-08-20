"""Tests for ``kodo.llms.meta._muse`` -- the Meta Muse LLM plugin.

Mirrors test_gpt.py's shape (the OpenAI plugin's equivalent, since Meta's
Model API is Responses-API-shaped too). Covers:
* :class:`MusePlugin` properties (``name``, ``supported_models``).
* :meth:`MusePlugin.cancel` sets the cancel event.
* :meth:`MusePlugin.stream_query` delegates to the internal stream.
* :meth:`MusePlugin.__raw_stream` parses Responses API stream events.
* The account-level "contributor" tier's model-id-suffix rewrite, both in
  the outbound API request and in the reported ``Usage.model``.
* :func:`_map_stop_reason`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

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
from kodo.llms.meta._muse import (
    _BASE_URL,
    _CONTRIBUTOR_SUFFIX,
    _DEFAULT_REASONING_EFFORT,
    _REASONING_EFFORTS,
    MusePlugin,
    _map_stop_reason,
    _reasoning_effort_for,
)

# ---------------------------------------------------------------------------
# _reasoning_effort_for -- pure
# ---------------------------------------------------------------------------


def test_reasoning_effort_forwards_every_valid_tier() -> None:
    """Muse Spark's own effort levels are the tier slugs -- forwarded verbatim."""
    for tier in sorted(_REASONING_EFFORTS):
        assert _reasoning_effort_for(tier) == tier


def test_reasoning_effort_none_falls_back_to_default() -> None:
    assert _reasoning_effort_for(None) == _DEFAULT_REASONING_EFFORT


def test_reasoning_effort_rejects_max_tier() -> None:
    """Muse Spark has no "max" level (its ladder tops out at xhigh)."""
    assert _reasoning_effort_for("max") == _DEFAULT_REASONING_EFFORT


def test_reasoning_effort_never_sends_none_level() -> None:
    """Meta rejects effort "none" with a 400 -- and kodo offers no off tier."""
    assert "none" not in _REASONING_EFFORTS
    assert _reasoning_effort_for("none") == _DEFAULT_REASONING_EFFORT


def test_reasoning_effort_accepts_exactly_the_registered_family_tiers() -> None:
    """Accepted set == the family catalog the server advertises (see test_gpt)."""
    assert set(cloud_thinking_tiers("meta")) == _REASONING_EFFORTS
    assert cloud_thinking_default_tier("meta") == _DEFAULT_REASONING_EFFORT


# ---------------------------------------------------------------------------
# _map_stop_reason -- pure
# ---------------------------------------------------------------------------


def _make_response(status: str = "completed", incomplete_reason: str | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    if incomplete_reason is not None:
        response.incomplete_details = MagicMock(reason=incomplete_reason)
    else:
        response.incomplete_details = None
    return response


def test_map_stop_reason_completed_no_tool_call_is_end_turn() -> None:
    assert _map_stop_reason(_make_response("completed"), made_tool_call=False) == "end_turn"


def test_map_stop_reason_completed_with_tool_call_is_tool_use() -> None:
    assert _map_stop_reason(_make_response("completed"), made_tool_call=True) == "tool_use"


def test_map_stop_reason_incomplete_max_output_tokens_is_max_tokens() -> None:
    response = _make_response("incomplete", incomplete_reason="max_output_tokens")
    assert _map_stop_reason(response, made_tool_call=False) == "max_tokens"


def test_map_stop_reason_incomplete_content_filter_is_incomplete() -> None:
    response = _make_response("incomplete", incomplete_reason="content_filter")
    assert _map_stop_reason(response, made_tool_call=False) == "incomplete"


def test_map_stop_reason_incomplete_with_no_details_is_incomplete() -> None:
    response = _make_response("incomplete")
    assert _map_stop_reason(response, made_tool_call=False) == "incomplete"


# ---------------------------------------------------------------------------
# MusePlugin -- properties
# ---------------------------------------------------------------------------


def test_muse_plugin_name() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    assert plugin.name == "meta"


def test_muse_plugin_supported_models() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    assert plugin.supported_models == ["muse-spark-1.2"]


def test_muse_plugin_defaults_to_non_contributor() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    assert plugin._MusePlugin__contributor is False


def test_muse_plugin_client_points_at_meta_base_url() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    assert str(plugin._MusePlugin__client.base_url).rstrip("/") == _BASE_URL


# ---------------------------------------------------------------------------
# MusePlugin -- cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_plugin_cancel_no_stream_id() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    await plugin.cancel("nonexistent-stream")  # should not raise


@pytest.mark.asyncio
async def test_muse_plugin_cancel_sets_event() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._MusePlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-1")
    assert event.is_set()


@pytest.mark.asyncio
async def test_muse_plugin_cancel_unknown_stream_is_noop() -> None:
    plugin = MusePlugin(api_key="test-key-123")
    event = asyncio.Event()
    plugin._MusePlugin__cancel_events["stream-1"] = event

    await plugin.cancel("stream-999")
    assert not event.is_set()


# ---------------------------------------------------------------------------
# MusePlugin -- stream_query (mocked internal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_stream_query_yields_from_inner() -> None:
    """stream_query delegates to __stream_with_retry which yields from __raw_stream."""
    plugin = MusePlugin(api_key="test-key-123")
    captured_args: dict[str, Any] = {}

    async def _fake_with_retry(**kwargs: Any) -> Any:
        captured_args.update(kwargs)

        class _FakeEvent:
            type = "response.output_text.delta"

        yield _FakeEvent()

    plugin._MusePlugin__stream_with_retry = _fake_with_retry

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    assert len(events) >= 1
    assert captured_args["stream_id"] == "s1"
    assert captured_args["model"] == "muse-spark-1.2"


# ---------------------------------------------------------------------------
# __raw_stream -- parsing the Responses API stream
# ---------------------------------------------------------------------------


def _make_text_delta(text: str, item_id: str = "item_1") -> Any:
    from openai.types.responses import ResponseTextDeltaEvent

    return ResponseTextDeltaEvent(
        type="response.output_text.delta",
        content_index=0,
        delta=text,
        item_id=item_id,
        logprobs=[],
        output_index=0,
        sequence_number=0,
    )


def _make_reasoning_delta(text: str, item_id: str = "item_1") -> Any:
    from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent

    return ResponseReasoningSummaryTextDeltaEvent(
        type="response.reasoning_summary_text.delta",
        delta=text,
        item_id=item_id,
        output_index=0,
        sequence_number=0,
        summary_index=0,
    )


def _make_function_tool_call(
    call_id: str, name: str, arguments: str, item_id: str | None = None
) -> Any:
    from openai.types.responses import ResponseFunctionToolCall

    return ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
        id=item_id,
    )


def _make_output_item_added(item: Any) -> Any:
    from openai.types.responses import ResponseOutputItemAddedEvent

    return ResponseOutputItemAddedEvent(
        type="response.output_item.added", item=item, output_index=0, sequence_number=0
    )


def _make_output_item_done(item: Any) -> Any:
    from openai.types.responses import ResponseOutputItemDoneEvent

    return ResponseOutputItemDoneEvent(
        type="response.output_item.done", item=item, output_index=0, sequence_number=0
    )


def _make_function_call_arg_delta(item_id: str, delta: str) -> Any:
    from openai.types.responses import ResponseFunctionCallArgumentsDeltaEvent

    return ResponseFunctionCallArgumentsDeltaEvent(
        type="response.function_call_arguments.delta",
        delta=delta,
        item_id=item_id,
        output_index=0,
        sequence_number=0,
    )


def _make_final_response(
    input_tokens: int = 10,
    output_tokens: int = 5,
    cached_tokens: int = 0,
    status: str = "completed",
) -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.input_tokens_details = MagicMock(cached_tokens=cached_tokens)
    final = MagicMock()
    final.usage = usage
    final.status = status
    final.incomplete_details = None
    return final


class _FakeResponseStreamCtx:
    """A proper async context manager mirroring test_gpt.py's equivalent."""

    def __init__(self, events: list, final: MagicMock | None = None) -> None:
        self._events = list(events)
        self._idx = 0
        self._final = final

    async def __aenter__(self) -> _FakeResponseStreamCtx:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _FakeResponseStreamCtx:
        return self

    async def __anext__(self) -> Any:
        if self._idx < len(self._events):
            event = self._events[self._idx]
            self._idx += 1
            return event
        raise StopAsyncIteration

    async def get_final_response(self) -> MagicMock:
        if self._final is not None:
            return self._final
        return _make_final_response()


def _make_plugin(contributor: bool = False) -> MusePlugin:
    return MusePlugin(api_key="test-key", contributor=contributor)


def _patch_client(
    plugin: MusePlugin, stream_events: list, final: MagicMock | None = None
) -> MagicMock:
    fake_stream = _FakeResponseStreamCtx(stream_events, final)
    mock_stream = MagicMock(return_value=fake_stream)
    plugin._MusePlugin__client.responses.stream = mock_stream  # type: ignore[method-assign]
    return mock_stream


def _not_cancelled() -> MagicMock:
    event = MagicMock(spec=asyncio.Event)
    event.is_set.return_value = False
    return event


@pytest.mark.asyncio
async def test_muse_raw_stream_yields_text_tokens() -> None:
    plugin = _make_plugin()
    events_in = [_make_text_delta("Hello"), _make_text_delta(" world")]
    final = _make_final_response()
    _patch_client(plugin, events_in, final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    token_events = [e for e in events if isinstance(e, TokenDelta)]
    assert len(token_events) == 2
    assert token_events[0].text == "Hello"
    assert token_events[1].text == " world"
    assert any(isinstance(e, TurnEnd) for e in events)


@pytest.mark.asyncio
async def test_muse_raw_stream_yields_reasoning_summary() -> None:
    plugin = _make_plugin()
    events_in = [_make_reasoning_delta("Let me think about this")]
    final = _make_final_response()
    _patch_client(plugin, events_in, final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    thinking_events = [e for e in events if isinstance(e, ThinkingDelta)]
    assert len(thinking_events) == 1
    assert thinking_events[0].text == "Let me think about this"


@pytest.mark.asyncio
async def test_muse_raw_stream_yields_tool_call() -> None:
    plugin = _make_plugin()
    tool_call = _make_function_tool_call(
        "call_1", "read_file", '{"path": "/foo/bar.py"}', item_id="item_1"
    )
    events_in = [
        _make_output_item_added(tool_call),
        _make_function_call_arg_delta("item_1", '{"path"'),
        _make_function_call_arg_delta("item_1", ': "/foo/bar.py"}'),
        _make_output_item_done(tool_call),
    ]
    final = _make_final_response()
    _patch_client(plugin, events_in, final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "read_file"
    assert tool_events[0].tool_use_id == "call_1"
    assert tool_events[0].tool_input == {"path": "/foo/bar.py"}

    arg_events = [e for e in events if isinstance(e, ToolCallArgDelta)]
    assert len(arg_events) == 2
    assert all(e.tool_name == "read_file" for e in arg_events)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_muse_raw_stream_tool_call_malformed_json() -> None:
    plugin = _make_plugin()
    tool_call = _make_function_tool_call(
        "call_1", "write_file", "not valid json {", item_id="item_1"
    )
    events_in = [_make_output_item_added(tool_call), _make_output_item_done(tool_call)]
    final = _make_final_response()
    _patch_client(plugin, events_in, final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
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
async def test_muse_raw_stream_cancel_stops_early() -> None:
    plugin = _make_plugin()
    cancel_event = asyncio.Event()
    cancel_event.set()
    final = _make_final_response()
    _patch_client(plugin, [_make_text_delta("partial")], final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=cancel_event,
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    token_events = [e for e in events if isinstance(e, TokenDelta)]
    assert len(token_events) == 0
    assert not any(isinstance(e, TurnEnd) for e in events)


@pytest.mark.asyncio
async def test_muse_raw_stream_usage_includes_cache_read_tokens() -> None:
    plugin = _make_plugin()
    final = _make_final_response(input_tokens=100, output_tokens=50, cached_tokens=20)
    _patch_client(plugin, [], final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert len(turn_ends) == 1
    usage: Usage = turn_ends[0].usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_write_tokens == 0
    assert usage.cache_read_tokens == 20
    assert usage.model == "muse-spark-1.2"


@pytest.mark.asyncio
async def test_muse_raw_stream_stop_reason_max_tokens() -> None:
    plugin = _make_plugin()
    final = _make_final_response(status="incomplete")
    final.incomplete_details = MagicMock(reason="max_output_tokens")
    _patch_client(plugin, [], final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_muse_raw_stream_no_tool_call_is_end_turn() -> None:
    plugin = _make_plugin()
    final = _make_final_response()
    _patch_client(plugin, [_make_text_delta("hi")], final)

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# __raw_stream -- fixed reasoning effort (no per-model table like OpenAI's)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_raw_stream_sends_fixed_reasoning_effort() -> None:
    plugin = _make_plugin()
    mock_stream = _patch_client(plugin, [], _make_final_response())

    async for _ in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level="xhigh",
    ):
        pass

    _, kwargs = mock_stream.call_args
    assert kwargs["reasoning"] == {"effort": "xhigh", "summary": "auto"}


# ---------------------------------------------------------------------------
# __raw_stream -- contributor tier model-id-suffix rewrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_raw_stream_standard_tier_sends_bare_model_id() -> None:
    plugin = _make_plugin(contributor=False)
    mock_stream = _patch_client(plugin, [], _make_final_response())

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    _, kwargs = mock_stream.call_args
    assert kwargs["model"] == "muse-spark-1.2"
    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    assert turn_ends[0].usage.model == "muse-spark-1.2"


@pytest.mark.asyncio
async def test_muse_raw_stream_contributor_tier_suffixes_model_id() -> None:
    plugin = _make_plugin(contributor=True)
    mock_stream = _patch_client(plugin, [], _make_final_response())

    events = []
    async for event in plugin._MusePlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="muse-spark-1.2",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level=None,
    ):
        events.append(event)

    _, kwargs = mock_stream.call_args
    assert kwargs["model"] == "muse-spark-1.2" + _CONTRIBUTOR_SUFFIX
    turn_ends = [e for e in events if isinstance(e, TurnEnd)]
    # The reported Usage.model carries the suffix too, so pricing
    # (kodo.llms.meta._usage.compute_cost) picks the discounted row.
    assert turn_ends[0].usage.model == "muse-spark-1.2-contributor"

"""Tests for ``kodo.llms.bedrock._bedrock`` -- the AWS Bedrock LLM plugin.

Shares the shape of test_openrouter.py (properties/cancel/stream_query
delegation, then a mocked-client pass over the whole streaming loop), but the
fixtures are Converse's own event stream rather than Chat Completions chunks,
and the client is a *blocking* boto3 stand-in iterated on a worker thread --
so these also exercise the thread/asyncio bridge in
``kodo.llms.bedrock._stream``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from kodo.llms._interface import (
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    TurnEnd,
)
from kodo.llms.bedrock._bedrock import BedrockPlugin, _map_stop_reason

_CREDENTIALS = json.dumps({"access_key_id": "AKIATEST", "secret_access_key": "s3cr3t"})


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEventStream:
    """A blocking iterable, like the botocore ``EventStream`` boto3 returns."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self.closed = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        yield from self._events

    def close(self) -> None:
        self.closed = True


class _FakeBedrockClient:
    """Stands in for a boto3 ``bedrock-runtime`` client."""

    def __init__(self, events: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.request: dict[str, Any] = {}
        self.stream = _FakeEventStream(events)

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.request = kwargs
        if self.error is not None:
            raise self.error
        return {"stream": self.stream}


def _plugin(events: list[dict[str, Any]], error: Exception | None = None) -> BedrockPlugin:
    plugin = BedrockPlugin(_CREDENTIALS, "us-east-1")
    plugin._BedrockPlugin__client = _FakeBedrockClient(events, error)
    return plugin


def _client(plugin: BedrockPlugin) -> _FakeBedrockClient:
    client: _FakeBedrockClient = plugin._BedrockPlugin__client
    return client


async def _collect(plugin: BedrockPlugin, **overrides: Any) -> list[Any]:
    kwargs: dict[str, Any] = {
        "stream_id": "s1",
        "model": "us.anthropic.claude-opus-5",
        "system": "You are helpful.",
        "messages": [],
        "tools": [],
        "cache_breakpoints": [],
    }
    kwargs.update(overrides)
    return [event async for event in plugin.stream_query(**kwargs)]


# ---------------------------------------------------------------------------
# _map_stop_reason -- pure
# ---------------------------------------------------------------------------


def test_map_stop_reason_end_turn() -> None:
    assert _map_stop_reason("end_turn") == "end_turn"


def test_map_stop_reason_tool_use() -> None:
    assert _map_stop_reason("tool_use") == "tool_use"


def test_map_stop_reason_max_tokens_is_preserved() -> None:
    """runtime/_engine/_watchdog.py reads this value verbatim."""
    assert _map_stop_reason("max_tokens") == "max_tokens"


def test_map_stop_reason_guardrail_is_end_turn() -> None:
    assert _map_stop_reason("guardrail_intervened") == "end_turn"


def test_map_stop_reason_none_is_end_turn() -> None:
    assert _map_stop_reason(None) == "end_turn"


# ---------------------------------------------------------------------------
# Properties / construction
# ---------------------------------------------------------------------------


def test_plugin_name() -> None:
    assert BedrockPlugin(_CREDENTIALS, "us-east-1").name == "bedrock"


def test_plugin_region_is_exposed() -> None:
    assert BedrockPlugin(_CREDENTIALS, "eu-central-1").region == "eu-central-1"


def test_supported_models_is_empty() -> None:
    """The catalog is fetched, and there is no router pseudo-model to name."""
    assert BedrockPlugin(_CREDENTIALS, "us-east-1").supported_models == []


def test_invalid_credentials_fail_at_construction() -> None:
    from kodo.llms.bedrock import InvalidCredentialsError

    with pytest.raises(InvalidCredentialsError):
        BedrockPlugin("AKIA-bare-key", "us-east-1")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_unknown_stream_is_noop() -> None:
    plugin = BedrockPlugin(_CREDENTIALS, "us-east-1")
    await plugin.cancel("nonexistent")  # should not raise


@pytest.mark.asyncio
async def test_cancel_sets_event() -> None:
    plugin = BedrockPlugin(_CREDENTIALS, "us-east-1")
    event = asyncio.Event()
    plugin._BedrockPlugin__cancel_events["stream-1"] = event
    await plugin.cancel("stream-1")
    assert event.is_set()


# ---------------------------------------------------------------------------
# Streaming loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_deltas_become_token_deltas() -> None:
    plugin = _plugin(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": " world"}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    events = await _collect(plugin)
    assert [e.text for e in events if isinstance(e, TokenDelta)] == ["Hello", " world"]
    assert isinstance(events[-1], TurnEnd)
    assert events[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_reasoning_content_becomes_thinking_delta() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"reasoningContent": {"text": "step one"}},
                }
            },
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    events = await _collect(plugin)
    assert [e.text for e in events if isinstance(e, ThinkingDelta)] == ["step one"]


@pytest.mark.asyncio
async def test_nested_reasoning_text_shape_is_also_read() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"reasoningContent": {"reasoningText": {"text": "deep"}}},
                }
            },
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    events = await _collect(plugin)
    assert [e.text for e in events if isinstance(e, ThinkingDelta)] == ["deep"]


@pytest.mark.asyncio
async def test_redacted_reasoning_yields_nothing() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"reasoningContent": {"redactedContent": b"\x00opaque"}},
                }
            },
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    events = await _collect(plugin)
    assert not any(isinstance(e, ThinkingDelta) for e in events)


@pytest.mark.asyncio
async def test_tool_use_is_accumulated_and_flushed() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tu-1", "name": "read_file"}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": '{"path"'}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": ': "a.py"}'}},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )
    events = await _collect(plugin)
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(calls) == 1
    assert calls[0].tool_use_id == "tu-1"
    assert calls[0].tool_name == "read_file"
    assert calls[0].tool_input == {"path": "a.py"}
    assert events[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_tool_arg_deltas_are_surfaced_for_progress() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tu-1", "name": "read_file"}},
                }
            },
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )
    events = await _collect(plugin)
    arg_deltas = [e for e in events if isinstance(e, ToolCallArgDelta)]
    # One announcing the call the moment its name is known, one per fragment.
    assert [d.tool_name for d in arg_deltas] == ["read_file", "read_file"]
    assert arg_deltas[0].text == ""
    assert arg_deltas[1].text == "{}"


@pytest.mark.asyncio
async def test_two_interleaved_tool_calls_are_kept_apart_by_index() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tu-1", "name": "first"}},
                }
            },
            {
                "contentBlockStart": {
                    "contentBlockIndex": 1,
                    "start": {"toolUse": {"toolUseId": "tu-2", "name": "second"}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 1,
                    "delta": {"toolUse": {"input": '{"b": 2}'}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": '{"a": 1}'}},
                }
            },
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )
    events = await _collect(plugin)
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert [c.tool_name for c in calls] == ["first", "second"]
    assert calls[0].tool_input == {"a": 1}
    assert calls[1].tool_input == {"b": 2}


@pytest.mark.asyncio
async def test_malformed_tool_arguments_are_preserved_raw() -> None:
    plugin = _plugin(
        [
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tu-1", "name": "t"}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": "{not json"}},
                }
            },
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )
    events = await _collect(plugin)
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert calls[0].tool_input == {"_raw": "{not json"}


@pytest.mark.asyncio
async def test_usage_is_read_off_metadata() -> None:
    plugin = _plugin(
        [
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 120,
                        "outputTokens": 34,
                        "cacheReadInputTokens": 12,
                        "cacheWriteInputTokens": 7,
                    },
                    "metrics": {"latencyMs": 900},
                }
            },
        ]
    )
    events = await _collect(plugin)
    usage = events[-1].usage
    assert usage.input_tokens == 120
    assert usage.output_tokens == 34
    assert usage.cache_read_tokens == 12
    assert usage.cache_write_tokens == 7
    assert usage.model == "us.anthropic.claude-opus-5"


@pytest.mark.asyncio
async def test_no_cost_is_ever_reported() -> None:
    """Bedrock reports no price, and kodo has no table for its catalog."""
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    events = await _collect(plugin)
    assert events[-1].usage.provider_reported_cost is None
    assert events[-1].usage.usd_cost == 0.0


@pytest.mark.asyncio
async def test_missing_metadata_leaves_zero_usage() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    events = await _collect(plugin)
    assert events[-1].usage.input_tokens == 0
    assert events[-1].usage.output_tokens == 0


# ---------------------------------------------------------------------------
# Request assembly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_goes_in_its_own_field() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, system="Be terse.")
    assert _client(plugin).request["system"] == [{"text": "Be terse."}]


@pytest.mark.asyncio
async def test_empty_system_prompt_is_omitted_entirely() -> None:
    """Converse rejects an empty system block rather than ignoring it."""
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, system="")
    assert "system" not in _client(plugin).request


@pytest.mark.asyncio
async def test_no_tools_omits_tool_config() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, tools=[])
    assert "toolConfig" not in _client(plugin).request


@pytest.mark.asyncio
async def test_thinking_level_reaches_additional_model_request_fields() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, model="us.anthropic.claude-opus-5", thinking_level="low")
    fields = _client(plugin).request["additionalModelRequestFields"]
    assert fields == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}


@pytest.mark.asyncio
async def test_non_claude_model_sends_no_additional_fields() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, model="amazon.nova-pro-v1:0", thinking_level="max")
    assert "additionalModelRequestFields" not in _client(plugin).request
    assert "inferenceConfig" not in _client(plugin).request


@pytest.mark.asyncio
async def test_deep_tier_raises_max_tokens() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, model="us.anthropic.claude-opus-5", thinking_level="max")
    assert _client(plugin).request["inferenceConfig"] == {"maxTokens": 32768}


@pytest.mark.asyncio
async def test_cache_breakpoints_are_ignored() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, cache_breakpoints=[0, 2])
    assert "cachePoint" not in json.dumps(_client(plugin).request)


# ---------------------------------------------------------------------------
# Cancellation / errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_mid_stream_stops_early_and_closes_the_stream() -> None:
    plugin = _plugin(
        [
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "one"}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "two"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    events: list[Any] = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="us.anthropic.claude-opus-5",
        system="",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)
        if isinstance(event, TokenDelta):
            await plugin.cancel("s1")

    assert [e.text for e in events if isinstance(e, TokenDelta)] == ["one"]
    assert not any(isinstance(e, TurnEnd) for e in events)
    # Closing the stream is what lets the worker thread exit instead of
    # parking until the model finishes generating.
    assert _client(plugin).stream.closed


@pytest.mark.asyncio
async def test_api_error_is_translated_and_raised_on_the_callers_task() -> None:
    from botocore.exceptions import ClientError

    from kodo.llms._provider_retry import UnrecoverableError

    error = ClientError(
        {
            "Error": {"Code": "AccessDeniedException", "Message": "no model access"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "ConverseStream",
    )
    plugin = _plugin([], error=error)
    with pytest.raises(UnrecoverableError):
        await _collect(plugin)


@pytest.mark.asyncio
async def test_cancel_event_is_cleaned_up_after_the_stream() -> None:
    plugin = _plugin([{"messageStop": {"stopReason": "end_turn"}}])
    await _collect(plugin, stream_id="s-cleanup")
    assert "s-cleanup" not in plugin._BedrockPlugin__cancel_events

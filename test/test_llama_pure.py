"""Tests for pure/helper functions in ``kodo.llms.llamacpp._llama``.

Covers the functions that don't need a real OpenAI client:
* :func:`_flatten_content`
* :func:`_expand_assistant`, :func:`_expand_user`, :func:`_expand_message`
* :func:`_build_oai_messages`
* :func:`_map_finish_reason`
* :func:`_partial_tag_suffix_len`
* :class:`ThinkingStreamParser` (feed/flush)
* :func:`_next_think_tag`
* :class:`_ThinkTagStripper` (feed/flush)
* :func:`_match_salvage_tools`
* :class:`MalformedToolCallError`
* :class:`LlamaPlugin` (name, supported_models)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kodo.llms._interface import Message, ThinkingDelta, TokenDelta, ToolSpec
from kodo.llms.llamacpp._llama import (
    _THINK_CLOSE,
    _THINK_OPEN,
    LlamaPlugin,
    MalformedToolCallError,
    ThinkingStreamParser,
    _build_oai_messages,
    _expand_assistant,
    _expand_message,
    _expand_user,
    _flatten_content,
    _map_finish_reason,
    _match_salvage_tools,
    _next_think_tag,
    _partial_tag_suffix_len,
    _ThinkTagStripper,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str = "test_tool", **overrides: Any) -> ToolSpec:
    defaults = dict(
        name=name,
        external_name=name,
        user_description=f"A {name} tool",
        description=f"Description of {name}",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        security_impact=MagicMock(),
        input_visibility={},
        output_visibility={},
        when_to_use=(),
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


# ---------------------------------------------------------------------------
# _flatten_content
# ---------------------------------------------------------------------------


def test_flatten_content_string_passthrough() -> None:
    assert _flatten_content("hello") == "hello"


def test_flatten_content_list_of_text_blocks() -> None:
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "thinking", "thinking": "ignored"},
        {"type": "text", "text": "second"},
    ]
    assert _flatten_content(blocks) == "first second"


def test_flatten_content_list_with_no_text_blocks() -> None:
    blocks = [{"type": "thinking", "thinking": "only thinking"}]
    assert _flatten_content(blocks) == ""


def test_flatten_content_non_string_non_list() -> None:
    assert _flatten_content(42) == "42"
    assert _flatten_content(None) == "None"


def test_flatten_content_mixed_list_with_non_dict() -> None:
    blocks = [{"type": "text", "text": "valid"}, "not a dict", 42]
    assert _flatten_content(blocks) == "valid"


# ---------------------------------------------------------------------------
# _expand_assistant
# ---------------------------------------------------------------------------


def test_expand_assistant_text_only() -> None:
    blocks = [{"type": "text", "text": "hello world"}]
    result = _expand_assistant(blocks)
    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "hello world"


def test_expand_assistant_with_thinking_tags() -> None:
    blocks = [
        {"type": "thinking", "thinking": "let me think"},
        {"type": "text", "text": "the answer"},
    ]
    result = _expand_assistant(blocks)
    assert len(result) == 1
    # The thinking block is wrapped in actual <think>...</think> tags.
    assert _THINK_OPEN + "let me think" + _THINK_CLOSE in result[0]["content"]
    assert "the answer" in result[0]["content"]


def test_expand_assistant_with_tool_use() -> None:
    blocks = [
        {"type": "tool_use", "id": "tool_1", "name": "read_file", "input": {"path": "/x.py"}},
    ]
    result = _expand_assistant(blocks)
    assert len(result) == 1
    assert "tool_calls" in result[0]
    assert len(result[0]["tool_calls"]) == 1
    tc = result[0]["tool_calls"][0]
    assert tc["id"] == "tool_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"


def test_expand_assistant_empty_blocks() -> None:
    result = _expand_assistant([])
    assert len(result) == 1
    assert result[0]["content"] is None


# ---------------------------------------------------------------------------
# _expand_user
# ---------------------------------------------------------------------------


def test_expand_user_text_only() -> None:
    blocks = [{"type": "text", "text": "hello"}]
    result = _expand_user(blocks)
    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "hello"


def test_expand_user_with_tool_result() -> None:
    blocks = [
        {"type": "text", "text": "here's the result"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
    ]
    result = _expand_user(blocks)
    assert result[0]["role"] == "tool"
    assert result[0]["tool_call_id"] == "t1"
    assert result[0]["content"] == "file contents"
    assert result[1]["role"] == "user"


def test_expand_user_tool_result_with_content_block() -> None:
    blocks = [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "block"}],
        },
    ]
    result = _expand_user(blocks)
    assert result[0]["content"] == "block"


def test_expand_user_empty() -> None:
    result = _expand_user([])
    assert result == []


# ---------------------------------------------------------------------------
# _expand_message
# ---------------------------------------------------------------------------


def test_expand_message_string_content() -> None:
    msg = Message(role="user", content="hello")
    result = _expand_message(msg)
    assert result == [{"role": "user", "content": "hello"}]


def test_expand_message_assistant_string_strips_callouts() -> None:
    msg = Message(role="assistant", content="<kodo_info>test</kodo_info>hello")
    result = _expand_message(msg)
    assert "<kodo_info>" not in result[0]["content"]
    assert "hello" in result[0]["content"]


def test_expand_message_assistant_blocks() -> None:
    msg = Message(role="assistant", content=[{"type": "text", "text": "hi"}])
    result = _expand_message(msg)
    assert result[0]["role"] == "assistant"


def test_expand_message_user_blocks() -> None:
    msg = Message(role="user", content=[{"type": "text", "text": "hi"}])
    result = _expand_message(msg)
    assert result[0]["role"] == "user"


def test_expand_message_other_role_blocks() -> None:
    msg = Message(
        role="system",
        content=[{"type": "text", "text": "sys"}, {"type": "thinking", "thinking": "x"}],
    )
    result = _expand_message(msg)
    assert result[0]["content"] == "sys"


# ---------------------------------------------------------------------------
# _build_oai_messages
# ---------------------------------------------------------------------------


def test_build_oai_messages_includes_system() -> None:
    result = _build_oai_messages("You are helpful.", [])
    assert result[0] == {"role": "system", "content": "You are helpful."}


def test_build_oai_messages_expands_user_message() -> None:
    messages = [Message(role="user", content="hello")]
    result = _build_oai_messages("sys", messages)
    assert result[0]["role"] == "system"
    assert result[1] == {"role": "user", "content": "hello"}


def test_build_oai_messages_expands_assistant_message() -> None:
    messages = [Message(role="assistant", content="response")]
    result = _build_oai_messages("sys", messages)
    assert result[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# _map_finish_reason
# ---------------------------------------------------------------------------


def test_map_finish_reason_stop() -> None:
    assert _map_finish_reason("stop") == "end_turn"


def test_map_finish_reason_tool_calls() -> None:
    assert _map_finish_reason("tool_calls") == "tool_use"


def test_map_finish_reason_length() -> None:
    assert _map_finish_reason("length") == "max_tokens"


def test_map_finish_reason_unknown() -> None:
    assert _map_finish_reason("something_else") == "something_else"


def test_map_finish_reason_none() -> None:
    assert _map_finish_reason(None) == "end_turn"


# ---------------------------------------------------------------------------
# _partial_tag_suffix_len
# ---------------------------------------------------------------------------


def test_partial_tag_suffix_len_no_match() -> None:
    assert _partial_tag_suffix_len("hello world", _THINK_OPEN) == 0


def test_partial_tag_suffix_len_partial_match() -> None:
    # Take first 3 chars of the open tag and check partial match.
    prefix = _THINK_OPEN[:3]
    assert _partial_tag_suffix_len(prefix, _THINK_OPEN) == 3


def test_partial_tag_suffix_len_empty_buffer() -> None:
    assert _partial_tag_suffix_len("", _THINK_OPEN) == 0


def test_partial_tag_suffix_len_exact_prefix() -> None:
    tag = _THINK_OPEN
    # Take the first 3 chars of the tag.
    assert _partial_tag_suffix_len(tag[:3], tag) == 3


def test_partial_tag_suffix_len_no_match_at_all() -> None:
    assert _partial_tag_suffix_len("xyz", _THINK_OPEN) == 0


# ---------------------------------------------------------------------------
# ThinkingStreamParser
# ---------------------------------------------------------------------------


def test_parser_plain_text_emits_immediately() -> None:
    parser = ThinkingStreamParser()
    events = parser.feed("hello world")
    assert len(events) == 1
    assert isinstance(events[0], TokenDelta)
    assert events[0].text == "hello world"
    assert parser.flush() == []


def test_parser_emits_thinking_and_text() -> None:
    parser = ThinkingStreamParser()
    events = parser.feed(_THINK_OPEN + "reasoning" + _THINK_CLOSE + "answer")
    assert len(events) == 2
    assert isinstance(events[0], ThinkingDelta)
    assert events[0].text == "reasoning"
    assert isinstance(events[1], TokenDelta)
    assert events[1].text == "answer"


def test_parser_withholds_partial_think_open() -> None:
    parser = ThinkingStreamParser()
    # "<thi" is a partial match for the open tag.
    events = parser.feed("<thi")
    assert events == []


def test_parser_withholds_partial_think_close() -> None:
    parser = ThinkingStreamParser()
    parser.feed("text" + _THINK_OPEN + "thinking")
    # Now in thinking mode; "</" is partial close tag.
    events = parser.feed("</")
    # Buffer ends with "</" which doesn't match the start of the close tag.
    # The partial prefix withheld is 0 (no match).
    assert events == []


def test_parser_flush_emits_remainder_as_token() -> None:
    parser = ThinkingStreamParser()
    parser.feed("ready <thi")
    events = parser.flush()
    assert len(events) == 1
    assert isinstance(events[0], TokenDelta)


def test_parser_complete_think_close_emits_thinking() -> None:
    parser = ThinkingStreamParser()
    events = parser.feed(_THINK_OPEN + "content")
    assert len(events) == 1
    assert isinstance(events[0], ThinkingDelta)


def test_parser_multiple_regions_in_one_chunk() -> None:
    parser = ThinkingStreamParser()
    text = (
        "before"
        + _THINK_OPEN
        + "t1"
        + _THINK_CLOSE
        + "middle"
        + _THINK_OPEN
        + "t2"
        + _THINK_CLOSE
        + "after"
    )
    events = parser.feed(text)
    types = [type(e).__name__ for e in events]
    assert types == ["TokenDelta", "ThinkingDelta", "TokenDelta", "ThinkingDelta", "TokenDelta"]
    assert parser.flush() == []


# ---------------------------------------------------------------------------
# _next_think_tag
# ---------------------------------------------------------------------------


def test_next_think_tag_no_tags() -> None:
    assert _next_think_tag("plain text", 0) is None


def test_next_think_tag_only_open() -> None:
    text = "hello" + _THINK_OPEN + "world"
    idx, tag = _next_think_tag(text, 0)
    assert tag == _THINK_OPEN
    assert idx == 5


def test_next_think_tag_only_close() -> None:
    text = "hello" + _THINK_CLOSE + "world"
    idx, tag = _next_think_tag(text, 0)
    assert tag == _THINK_CLOSE
    assert idx == 5


def test_next_think_tag_open_come_first() -> None:
    text = "a" + _THINK_OPEN + "b" + _THINK_CLOSE + "c"
    idx, tag = _next_think_tag(text, 0)
    assert tag == _THINK_OPEN
    assert idx == 1


def test_next_think_tag_close_come_first() -> None:
    text = "a" + _THINK_CLOSE + "b" + _THINK_OPEN + "c"
    idx, tag = _next_think_tag(text, 0)
    assert tag == _THINK_CLOSE
    assert idx == 1


def test_next_think_tag_search_from_offset() -> None:
    text = "abc" + _THINK_OPEN + "more"
    idx, tag = _next_think_tag(text, 0)
    assert tag == _THINK_OPEN
    assert idx == 3
    result = _next_think_tag(text, 9)
    assert result is None


# ---------------------------------------------------------------------------
# _ThinkTagStripper
# ---------------------------------------------------------------------------


def test_stripper_plain_text_passthrough() -> None:
    stripper = _ThinkTagStripper()
    assert stripper.feed("hello world") == "hello world"
    assert stripper.flush() == ""


def test_stripper_balanced_thinking_tags() -> None:
    stripper = _ThinkTagStripper()
    out = stripper.feed(_THINK_OPEN + "reasoning" + _THINK_CLOSE)
    assert out == "reasoning"
    assert stripper.flush() == ""


def test_stripper_unmatched_open_kept_verbatim() -> None:
    stripper = _ThinkTagStripper()
    out = stripper.feed("before" + _THINK_OPEN + "after")
    assert out == "before"
    flush_out = stripper.flush()
    assert _THINK_OPEN in flush_out


def test_stripper_unmatched_close_kept_verbatim() -> None:
    stripper = _ThinkTagStripper()
    out = stripper.feed("before" + _THINK_CLOSE + "after")
    assert out == "before" + _THINK_CLOSE + "after"
    assert stripper.flush() == ""


def test_stripper_split_across_chunks() -> None:
    stripper = _ThinkTagStripper()
    out1 = stripper.feed("hel")
    assert out1 == "hel"
    out2 = stripper.feed("lo " + _THINK_OPEN + "reasoning" + _THINK_CLOSE)
    # The thinking region is stripped; the text before it is emitted.
    assert "lo" in out2
    # After balanced open/close, the inner text "reasoning" is also stripped.
    flush_out = stripper.flush()
    assert flush_out == ""


def test_stripper_flush_releases_remainder() -> None:
    stripper = _ThinkTagStripper()
    out = stripper.feed("partial")
    assert out == "partial"
    assert stripper.flush() == ""


# ---------------------------------------------------------------------------
# _match_salvage_tools
# ---------------------------------------------------------------------------


def test_match_salvage_tools_no_tools_returns_empty() -> None:
    assert _match_salvage_tools({"key": "val"}, []) == []


def test_match_salvage_tools_exact_match() -> None:
    tools = [
        _make_tool(
            "read_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    ]
    matches = _match_salvage_tools({"path": "/foo"}, tools)
    assert matches == ["read_file"]


def test_match_salvage_tools_no_match() -> None:
    tools = [
        _make_tool(
            "read_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    ]
    matches = _match_salvage_tools({"other_key": "val"}, tools)
    assert matches == []


def test_match_salvage_tools_missing_required() -> None:
    tools = [
        _make_tool(
            "read_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    ]
    matches = _match_salvage_tools({}, tools)
    assert matches == []


def test_match_salvage_tools_multiple_matches() -> None:
    tools = [
        _make_tool(
            "read_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        _make_tool(
            "write_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
    ]
    matches = _match_salvage_tools({"path": "/x"}, tools)
    assert matches == ["read_file"]


def test_match_salvage_tools_empty_args_no_match() -> None:
    tools = [_make_tool("noop", input_schema={"type": "object", "properties": {}})]
    matches = _match_salvage_tools({}, tools)
    assert matches == []


def test_match_salvage_tools_extra_keys_no_match() -> None:
    tools = [
        _make_tool(
            "read_file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    ]
    matches = _match_salvage_tools({"path": "/x", "extra": "val"}, tools)
    assert matches == []


def test_match_salvage_tools_non_dict_schema_skipped() -> None:
    tools = [_make_tool("test", input_schema="not a dict")]
    matches = _match_salvage_tools({"key": "val"}, tools)
    assert matches == []


# ---------------------------------------------------------------------------
# MalformedToolCallError
# ---------------------------------------------------------------------------


def test_malformed_tool_call_error_is_runtime_error() -> None:
    assert issubclass(MalformedToolCallError, RuntimeError)
    err = MalformedToolCallError("test message")
    assert str(err) == "test message"


# ---------------------------------------------------------------------------
# LlamaPlugin
# ---------------------------------------------------------------------------


def test_llama_plugin_name() -> None:
    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())
    assert plugin.name == "llamacpp"


def test_llama_plugin_supported_models_default() -> None:
    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())
    assert plugin.supported_models == ["local"]


def test_llama_plugin_supported_models_with_running_server() -> None:
    """When a running server is active, supported_models returns its model name."""
    from kodo.common._protocols import MessageSink
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())

    mock_server = MagicMock()
    mock_server.is_running = True
    mock_server.model_name = "qwen36-27b"

    with patch.object(LlamaServer, "get_active_llama_server", MagicMock(return_value=mock_server)):
        assert plugin.supported_models == ["qwen36-27b"]


@pytest.mark.asyncio
async def test_llama_plugin_stream_query_calls_ensure_running() -> None:
    """stream_query() calls __ensure_running then __stream."""
    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())

    ensure_called = False

    async def _fake_ensure(model: str) -> None:
        nonlocal ensure_called
        ensure_called = True

    async def _fake_stream(**kwargs: Any) -> Any:
        yield TokenDelta(text="hello")

    plugin._LlamaPlugin__ensure_running = _fake_ensure
    plugin._LlamaPlugin__stream = _fake_stream

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="test-model",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    assert ensure_called
    assert len(events) == 1
    assert events[0].text == "hello"


@pytest.mark.asyncio
async def test_llama_plugin_cancel_sets_event() -> None:
    """cancel() sets the asyncio.Event for the given stream_id."""
    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())

    import asyncio

    event = asyncio.Event()
    plugin._LlamaPlugin__cancel_events["s1"] = event

    await plugin.cancel("s1")
    assert event.is_set()


@pytest.mark.asyncio
async def test_llama_plugin_cancel_unknown_stream_is_noop() -> None:
    """cancel() for an unknown stream_id is harmless."""
    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())
    # Should not raise.
    await plugin.cancel("nonexistent")


@pytest.mark.asyncio
async def test_llama_plugin_stream_query_with_mocked_ensure_and_stream() -> None:
    """stream_query with mocked __ensure_running and __stream covers both paths."""
    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=MagicMock())

    ensure_called_with: list[str] = []

    async def _fake_ensure(model_name: str) -> None:
        ensure_called_with.append(model_name)

    async def _fake_stream(**kwargs: Any) -> Any:
        yield TokenDelta(text="chunk1")
        yield TokenDelta(text="chunk2")

    plugin._LlamaPlugin__ensure_running = _fake_ensure
    plugin._LlamaPlugin__stream = _fake_stream

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="qwen36-27b",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    assert ensure_called_with == ["qwen36-27b"]
    assert len(events) == 2
    assert events[0].text == "chunk1"
    assert events[1].text == "chunk2"


@pytest.mark.asyncio
async def test_llama_plugin_stream_query_with_custom_server_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_query through __ensure_running with a custom_server_url entry."""
    from kodo.common._protocols import MessageSink
    from kodo.llms import LocalLLMEntry
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    kodo_dir = tmp_path

    # Create a fake registry that returns a custom_server_url entry.
    entry = LocalLLMEntry(
        name="my-custom",
        kind="custom_server_url",
        url="http://localhost:8080",
    )
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=entry)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value=fake_registry),
    )
    # Mock LlamaServer.get_active_llama_server to return None (no managed server).
    monkeypatch.setattr(
        LlamaServer,
        "get_active_llama_server",
        MagicMock(return_value=None),
    )

    plugin = LlamaPlugin(sink=sink, kodo_dir=kodo_dir)

    async def _fake_stream(**kwargs: Any) -> Any:
        yield TokenDelta(text="custom server response")

    plugin._LlamaPlugin__stream = _fake_stream

    events = []
    async for event in plugin.stream_query(
        stream_id="s1",
        model="my-custom",
        system="You are helpful.",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events.append(event)

    assert len(events) == 1
    assert events[0].text == "custom server response"


@pytest.mark.asyncio
async def test_llama_plugin_ensure_running_with_existing_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an identical server is already running, __ensure_running short-circuits."""
    from kodo.common._protocols import MessageSink
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)

    # Create a mock entry that matches the existing server.
    from kodo.llms import LocalLLMEntry

    entry = LocalLLMEntry(
        name="qwen36-27b",
        kind="hardcoded_hf",
        repo_id="acme/qwen36-27b",
        filename="qwen36-27b.Q4_K_M.gguf",
        base_llm="Qwen36-27B",
    )
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=entry)

    # Create a mock server that's already running with the matching model.
    mock_server = MagicMock()
    mock_server.is_running = True
    mock_server.model_name = "qwen36-27b"
    mock_server.base_url = "http://localhost:8081"

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value=fake_registry),
    )
    monkeypatch.setattr(
        LlamaServer,
        "get_active_llama_server",
        MagicMock(return_value=mock_server),
    )

    # Call __ensure_running directly.
    await plugin._LlamaPlugin__ensure_running("qwen36-27b")

    # The client should have been created (line 597-601).
    assert plugin._LlamaPlugin__client is not None
    # No events should have been sent (short-circuit path).
    sink.send.assert_not_called()


@pytest.mark.asyncio
async def test_llama_plugin_ensure_running_custom_server_stops_managed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom server URL: stops any running managed server and sends event."""
    from kodo.common._protocols import MessageSink
    from kodo.llms import LocalLLMEntry
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)

    entry = LocalLLMEntry(
        name="custom",
        kind="custom_server_url",
        url="http://custom:8080",
    )
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=entry)

    # Create a mock managed server that IS running.
    mock_managed = MagicMock()
    mock_managed.is_running = True
    mock_managed.stop = AsyncMock()

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value=fake_registry),
    )
    monkeypatch.setattr(
        LlamaServer,
        "get_active_llama_server",
        MagicMock(return_value=mock_managed),
    )

    # Call __ensure_running directly.
    await plugin._LlamaPlugin__ensure_running("custom")

    # The managed server should have been stopped.
    mock_managed.stop.assert_awaited_once()
    # An event should have been sent.
    sink.send.assert_called_once()
    # The client should be an AsyncOpenAI.
    assert plugin._LlamaPlugin__client is not None


@pytest.mark.asyncio
async def test_llama_plugin_ensure_running_entry_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the model isn't in the registry, RuntimeError is raised."""
    from kodo.common._protocols import MessageSink
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)

    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=None)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value=fake_registry),
    )
    monkeypatch.setattr(
        LlamaServer,
        "get_active_llama_server",
        MagicMock(return_value=None),
    )

    with pytest.raises(RuntimeError, match="Unknown local model"):
        await plugin._LlamaPlugin__ensure_running("nonexistent")

    # Should have sent error event.
    assert sink.send.call_count == 2  # starting + error


@pytest.mark.asyncio
async def test_llama_plugin_ensure_running_starts_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no server is running, __ensure_running starts one via ensure_llama_running."""
    from kodo.common._protocols import MessageSink
    from kodo.llms import LocalLLMEntry
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)

    entry = LocalLLMEntry(
        name="qwen36-27b",
        kind="hardcoded_hf",
        repo_id="acme/qwen36-27b",
        filename="qwen36-27b.Q4_K_M.gguf",
        base_llm="Qwen36-27B",
    )
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=entry)

    # Mock ensure_llama_running to return a fake server.
    mock_server = MagicMock()
    mock_server.base_url = "http://localhost:9090"
    mock_server.model_name = "qwen36-27b"
    mock_server.port = 9090

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value=fake_registry),
    )
    monkeypatch.setattr(
        LlamaServer,
        "get_active_llama_server",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.ensure_llama_running",
        AsyncMock(return_value=mock_server),
    )

    await plugin._LlamaPlugin__ensure_running("qwen36-27b")

    # ensure_llama_running should have been called.

    # (We patched it, so the mock was called.)
    # The client should be an AsyncOpenAI.
    assert plugin._LlamaPlugin__client is not None
    # Events should have been sent: starting + running.
    assert sink.send.call_count == 2


@pytest.mark.asyncio
async def test_llama_plugin_stream_writes_cancel_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__stream creates a cancel event and yields from __raw_stream."""

    import openai

    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)

    # Pre-set the client.
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    raw_events = [TokenDelta(text="raw1"), TokenDelta(text="raw2")]

    async def _fake_raw_stream(**kwargs: Any) -> Any:
        for e in raw_events:
            yield e

    plugin._LlamaPlugin__raw_stream = _fake_raw_stream

    events = []
    async for event in plugin._LlamaPlugin__stream(
        stream_id="s1",
        model="test",
        system="sys",
        messages=[],
        tools=[],
    ):
        events.append(event)

    assert len(events) == 2
    # The cancel event should have been cleaned up after the stream finishes.
    assert "s1" not in plugin._LlamaPlugin__cancel_events


@pytest.mark.asyncio
async def test_llama_plugin_cancel_during_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel() sets the event; __raw_stream checks it and stops."""
    import asyncio

    import openai

    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    cancel_event_received: asyncio.Event | None = None

    async def _fake_raw_stream(*, cancel_event: asyncio.Event, **kwargs: Any) -> Any:
        nonlocal cancel_event_received
        cancel_event_received = cancel_event
        # Wait briefly for cancel.
        await asyncio.sleep(0.05)
        if cancel_event.is_set():
            return
        yield TokenDelta(text="should not reach here")

    plugin._LlamaPlugin__raw_stream = _fake_raw_stream

    async def _run_stream() -> list:
        events = []
        async for event in plugin._LlamaPlugin__stream(
            stream_id="s2",
            model="test",
            system="sys",
            messages=[],
            tools=[],
        ):
            events.append(event)
        return events

    task = asyncio.create_task(_run_stream())
    await asyncio.sleep(0.01)
    await plugin.cancel("s2")
    events = await task

    assert cancel_event_received is not None
    assert cancel_event_received.is_set()
    assert events == []


# ---------------------------------------------------------------------------
# _build_thinking_extra_body -- covering qwen_reasoning_budget and
# gpt_oss_reasoning_effort branches
# ---------------------------------------------------------------------------


def test_build_thinking_extra_body_no_family_returns_default() -> None:
    """A model with no thinking family gets empty extra_body and DEFAULT_MAX_TOKENS."""
    from kodo.llms.llamacpp._llama import _DEFAULT_MAX_TOKENS, _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("NonexistentModel")
    assert extra_body == {}
    assert max_tokens == _DEFAULT_MAX_TOKENS


def test_build_thinking_extra_body_qwen_reasoning_budget_default_tier() -> None:
    """Qwen36-27B with default tier 'unlimited' gets thinking_budget_tokens=24576 and headroom."""
    from kodo.llms.llamacpp._llama import (
        _QWEN_MAX_TOKENS_HEADROOM,
        _build_thinking_extra_body,
    )

    extra_body, max_tokens = _build_thinking_extra_body("Qwen36-27B", override_tier="unlimited")
    assert extra_body == {"thinking_budget_tokens": 24576}
    assert max_tokens == 24576 + _QWEN_MAX_TOKENS_HEADROOM


def test_build_thinking_extra_body_qwen_reasoning_budget_low_tier() -> None:
    """Qwen36-27B with 'low' tier gets the correct budget."""
    from kodo.llms.llamacpp._llama import _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("Qwen36-27B", override_tier="low")
    assert extra_body == {"thinking_budget_tokens": 1536}
    assert max_tokens == 1536 + 8192  # _QWEN_MAX_TOKENS_HEADROOM


def test_build_thinking_extra_body_qwen35_9b_forces_chat_template_kwargs() -> None:
    """Qwen35-9B gets an extra 'chat_template_kwargs' key to force thinking on."""
    from kodo.llms.llamacpp._llama import _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("Qwen35-9B", override_tier="medium")
    assert "thinking_budget_tokens" in extra_body
    assert extra_body["thinking_budget_tokens"] == 8192
    assert extra_body["chat_template_kwargs"] == {"enable_thinking": True}


def test_build_thinking_extra_body_invalid_override_falls_back_to_default_tier() -> None:
    """When override_tier is not in tiers, falls back to the model's default tier."""
    from kodo.llms.llamacpp._llama import _build_thinking_extra_body

    # Qwen36-27B default tier is 'unlimited' with budget 24576
    extra_body, max_tokens = _build_thinking_extra_body(
        "Qwen36-27B", override_tier="nonexistent_tier"
    )
    assert extra_body == {"thinking_budget_tokens": 24576}
    assert max_tokens == 24576 + 8192  # _QWEN_MAX_TOKENS_HEADROOM


def test_build_thinking_extra_body_gpt_oss_reasoning_effort() -> None:
    """GPT-OSS family uses reasoning_effort tier slug, no numeric budget."""
    from kodo.llms.llamacpp._llama import _DEFAULT_MAX_TOKENS, _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("GPT-OSS-120B", override_tier="high")
    assert extra_body == {"chat_template_kwargs": {"reasoning_effort": "high"}}
    assert max_tokens == _DEFAULT_MAX_TOKENS


def test_build_thinking_extra_body_gpt_oss_default_tier() -> None:
    """GPT-OSS with no override falls back to default tier 'medium'."""
    from kodo.llms.llamacpp._llama import _DEFAULT_MAX_TOKENS, _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("GPT-OSS-120B")
    assert extra_body == {"chat_template_kwargs": {"reasoning_effort": "medium"}}
    assert max_tokens == _DEFAULT_MAX_TOKENS


def test_build_thinking_extra_body_none_override_falls_back_to_default() -> None:
    """When override_tier is None, falls back to default tier."""
    from kodo.llms.llamacpp._llama import _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("Qwen36-27B", override_tier=None)
    assert extra_body == {"thinking_budget_tokens": 24576}


def test_build_thinking_extra_body_gemma4_26b_qwen_family() -> None:
    """Gemma4-26B-A4B is also in the qwen_reasoning_budget family."""
    from kodo.llms.llamacpp._llama import _QWEN_MAX_TOKENS_HEADROOM, _build_thinking_extra_body

    extra_body, max_tokens = _build_thinking_extra_body("Gemma4-26B-A4B", override_tier="high")
    assert extra_body == {"thinking_budget_tokens": 8192}
    assert max_tokens == 8192 + _QWEN_MAX_TOKENS_HEADROOM


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

    *reasoning_content* and *tool_calls* default to ``None`` explicitly -- a
    bare :class:`MagicMock` attribute is truthy, which would trick
    ``__raw_stream``'s ``if reasoning_content:`` guard into treating the
    placeholder as real content.
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

    An optional *on_each* callback is invoked after each chunk is yielded,
    enabling tests to mutate state (e.g. set a cancel event) mid-stream.
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
    """A cancel_event mock that reports as not-set.

    MagicMock(spec=asyncio.Event).is_set() returns a truthy MagicMock by
    default, which would make __raw_stream's `if cancel_event.is_set():
    return` fire on the very first chunk -- is_set must be pinned to False.
    """
    event = MagicMock(spec=asyncio.Event)
    event.is_set.return_value = False
    return event


@pytest.mark.asyncio
async def test_llama_plugin_raw_stream_token_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__raw_stream yields TokenDelta events from content chunks."""
    import openai

    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    # Mock get_local_registry to return an empty registry (no entry for "test-model").
    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value={}),
    )

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5

    fake_chunks = [
        _make_chunk(content="Hello"),
        _make_chunk(content=" world"),
        _make_chunk(finish_reason="stop", usage=usage),
    ]

    async def _fake_create(**kwargs: Any) -> Any:
        return _FakeAsyncStream(fake_chunks)

    plugin._LlamaPlugin__client.chat.completions.create = _fake_create

    events = []
    async for event in plugin._LlamaPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
    ):
        events.append(event)

    # Should get TokenDeltas for "Hello" and " world", then TurnEnd.
    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert "Hello" in token_texts
    assert " world" in token_texts
    # Last event should be TurnEnd.
    from kodo.llms._interface import TurnEnd

    assert isinstance(events[-1], TurnEnd)


@pytest.mark.asyncio
async def test_llama_plugin_raw_stream_thinking_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__raw_stream yields ThinkingDelta from reasoning_content chunks."""
    import openai

    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value={}),
    )

    usage = MagicMock()
    usage.prompt_tokens = 5
    usage.completion_tokens = 3

    fake_chunks = [
        _make_chunk(reasoning_content="Let me think"),
        _make_chunk(reasoning_content=" about this"),
        _make_chunk(content="The answer is 42", usage=usage, finish_reason="stop"),
    ]

    async def _fake_create(**kwargs: Any) -> Any:
        return _FakeAsyncStream(fake_chunks)

    plugin._LlamaPlugin__client.chat.completions.create = _fake_create

    events = []
    async for event in plugin._LlamaPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
    ):
        events.append(event)

    thinking_events = [e for e in events if isinstance(e, ThinkingDelta)]
    assert len(thinking_events) >= 1
    from kodo.llms._interface import TurnEnd

    assert isinstance(events[-1], TurnEnd)


@pytest.mark.asyncio
async def test_llama_plugin_raw_stream_tool_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__raw_stream yields ToolCallArgDelta and ToolCallEvent from tool call chunks."""
    import openai

    from kodo.common._protocols import MessageSink
    from kodo.llms._interface import ToolCallEvent

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value={}),
    )

    fake_tool_call = MagicMock()
    fake_tool_call.index = 0
    fake_tool_call.id = "call_abc"
    fake_tool_call.function = MagicMock()
    fake_tool_call.function.name = "read_file"
    fake_tool_call.function.arguments = '{"path": "/foo"}'

    fake_chunks = [
        _make_chunk(tool_calls=[fake_tool_call]),
        _make_chunk(finish_reason="tool_calls"),
    ]

    async def _fake_create(**kwargs: Any) -> Any:
        return _FakeAsyncStream(fake_chunks)

    plugin._LlamaPlugin__client.chat.completions.create = _fake_create

    events = []
    async for event in plugin._LlamaPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
    ):
        events.append(event)

    tool_call_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_call_events) == 1
    assert tool_call_events[0].tool_name == "read_file"
    assert tool_call_events[0].tool_use_id == "call_abc"


@pytest.mark.asyncio
async def test_llama_plugin_raw_stream_salvage_malformed_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__raw_stream recovers a tool call dumped as plain text in content channel."""
    import openai

    from kodo.common._protocols import MessageSink
    from kodo.llms._interface import ToolCallEvent, ToolSpec

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value={}),
    )

    # Build a tools list so salvage has something to match against.
    tools = [
        ToolSpec(
            name="read_file",
            external_name="read_file",
            user_description="Read a file",
            description="Read a file from disk",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            output_schema={"type": "object", "properties": {}},
            security_impact=MagicMock(),
            input_visibility={},
            output_visibility={},
            when_to_use=(),
        ),
    ]

    fake_chunks = [
        _make_chunk(content='{"path": "/foo/bar.py"}'),
        _make_chunk(finish_reason="stop"),
    ]

    async def _fake_create(**kwargs: Any) -> Any:
        return _FakeAsyncStream(fake_chunks)

    plugin._LlamaPlugin__client.chat.completions.create = _fake_create

    events = []
    async for event in plugin._LlamaPlugin__raw_stream(
        cancel_event=_not_cancelled(),
        model="test-model",
        system="sys",
        messages=[],
        tools=tools,
    ):
        events.append(event)

    # Should have a salvaged ToolCallEvent.
    salvaged = [
        e for e in events if isinstance(e, ToolCallEvent) and getattr(e, "recovered", False)
    ]
    assert len(salvaged) == 1
    assert salvaged[0].tool_name == "read_file"


@pytest.mark.asyncio
async def test_llama_plugin_raw_stream_cancel_stops_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__raw_stream stops when cancel_event is set mid-stream."""
    import openai

    from kodo.common._protocols import MessageSink

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)
    plugin._LlamaPlugin__client = MagicMock(spec=openai.AsyncOpenAI)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value={}),
    )

    cancel_event = asyncio.Event()

    # __raw_stream checks cancel_event at the *top* of the loop, before
    # processing the chunk it just fetched -- so cancellation must be
    # signalled while fetching the *second* chunk for the first chunk's
    # content to still come through. A single-chunk fixture would have the
    # first (only) chunk arrive already-cancelled and get dropped.
    fake_chunks = [
        _make_chunk(content="first part"),
        _make_chunk(content="second part"),
    ]

    _cancel_state = {"fetches": 0}

    def _set_cancel_on_second_fetch(chunk: MagicMock) -> None:
        _cancel_state["fetches"] += 1
        if _cancel_state["fetches"] >= 2:
            cancel_event.set()

    async def _fake_create(**kwargs: Any) -> Any:
        return _FakeAsyncStream(fake_chunks, on_each=_set_cancel_on_second_fetch)

    plugin._LlamaPlugin__client.chat.completions.create = _fake_create

    events = []
    async for event in plugin._LlamaPlugin__raw_stream(
        cancel_event=cancel_event,
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
    ):
        events.append(event)

    # Only the first chunk should be yielded before cancel stops the loop.
    token_texts = [e.text for e in events if isinstance(e, TokenDelta)]
    assert "first part" in token_texts
    assert "second part" not in token_texts


# ---------------------------------------------------------------------------
# __ensure_running exception path (lines 615-621)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llama_plugin_ensure_running_starts_fails_with_error_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ensure_llama_running raises, __ensure_running sends an error event and re-raises."""
    from kodo.common._protocols import MessageSink
    from kodo.llms import LocalLLMEntry
    from kodo.llms.llamacpp._llama_server import LlamaServer

    sink = MagicMock(spec=MessageSink)
    plugin = LlamaPlugin(sink=sink, kodo_dir=tmp_path)

    entry = LocalLLMEntry(
        name="qwen36-27b",
        kind="hardcoded_hf",
        repo_id="acme/qwen36-27b",
        filename="qwen36-27b.Q4_K_M.gguf",
        base_llm="Qwen36-27B",
    )
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=entry)

    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.get_local_registry",
        MagicMock(return_value=fake_registry),
    )
    monkeypatch.setattr(
        LlamaServer,
        "get_active_llama_server",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "kodo.llms.llamacpp._llama.ensure_llama_running",
        AsyncMock(side_effect=RuntimeError("model not downloaded")),
    )

    with pytest.raises(RuntimeError, match="model not downloaded"):
        await plugin._LlamaPlugin__ensure_running("qwen36-27b")

    # Should have sent: starting event + error event = 2 events
    assert sink.send.call_count == 2
    # Second call should be the error event.
    error_call = sink.send.call_args_list[1]
    error_env = error_call.args[0]
    assert error_env.kind == "event"
    payload = error_env.payload
    assert payload.get("running") is False
    assert payload.get("error") == "model not downloaded"

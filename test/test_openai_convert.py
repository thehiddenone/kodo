"""Behavior tests for kodo.llms.openai._convert.

Mirrors test_cache.py's shape (the Anthropic plugin's equivalent conversion
module), covering build_input_items' item-shape mapping and build_tool_defs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from kodo.llms._interface import Message, ToolSpec
from kodo.llms.openai._convert import build_input_items, build_tool_defs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str = "test_tool", **overrides: Any) -> ToolSpec:
    defaults = dict(
        name=name,
        external_name=name,
        user_description=f"A {name} tool",
        description=f"Description of {name}",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        output_schema={"type": "object", "properties": {}},
        security_impact=MagicMock(),
        input_visibility={},
        output_visibility={},
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


# ---------------------------------------------------------------------------
# build_input_items -- plain string content
# ---------------------------------------------------------------------------


def test_build_input_items_empty_messages() -> None:
    assert build_input_items([]) == []


def test_build_input_items_string_user_message() -> None:
    result = build_input_items([Message(role="user", content="Hello")])
    assert result == [{"type": "message", "role": "user", "content": "Hello"}]


def test_build_input_items_string_assistant_message() -> None:
    result = build_input_items([Message(role="assistant", content="Hi there")])
    assert result == [{"type": "message", "role": "assistant", "content": "Hi there"}]


def test_build_input_items_strips_callout_from_assistant_string_content() -> None:
    messages = [Message(role="assistant", content="Done. <kodo>All tests pass.</kodo>")]
    result = build_input_items(messages)
    assert result[0]["content"] == "Done. "


def test_build_input_items_does_not_strip_callout_from_user_string_content() -> None:
    messages = [Message(role="user", content="What does <kodo_info>x</kodo_info> mean?")]
    result = build_input_items(messages)
    assert result[0]["content"] == "What does <kodo_info>x</kodo_info> mean?"


def test_build_input_items_empty_string_message_omitted() -> None:
    """An assistant message that becomes empty after callout-stripping is dropped."""
    messages = [Message(role="assistant", content="<kodo_info>only a callout</kodo_info>")]
    assert build_input_items(messages) == []


# ---------------------------------------------------------------------------
# build_input_items -- list-of-blocks content: text blocks
# ---------------------------------------------------------------------------


def test_build_input_items_user_text_block() -> None:
    blocks = [{"type": "text", "text": "Hello"}]
    result = build_input_items([Message(role="user", content=blocks)])
    assert result == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}
    ]


def test_build_input_items_assistant_text_block_uses_input_text_type() -> None:
    """Replayed assistant text also uses 'input_text' -- the Responses API's

    input item content list has no 'output_text' variant; that type only
    appears in a live Response's own output, never as something resent back.
    """
    blocks = [{"type": "text", "text": "Sure, here you go."}]
    result = build_input_items([Message(role="assistant", content=blocks)])
    assert result[0]["content"] == [{"type": "input_text", "text": "Sure, here you go."}]


def test_build_input_items_strips_callout_from_assistant_text_block() -> None:
    blocks = [{"type": "text", "text": "Indexing done. <kodo_info>moving on</kodo_info> bye"}]
    result = build_input_items([Message(role="assistant", content=blocks)])
    assert result[0]["content"][0]["text"] == "Indexing done.  bye"


def test_build_input_items_multiple_consecutive_text_blocks_kept_separate() -> None:
    """Consecutive text blocks stay as separate content entries (no string join)."""
    blocks = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    result = build_input_items([Message(role="user", content=blocks)])
    assert result[0]["content"] == [
        {"type": "input_text", "text": "A"},
        {"type": "input_text", "text": "B"},
    ]


# ---------------------------------------------------------------------------
# build_input_items -- tool_use / tool_result -> flat top-level items
# ---------------------------------------------------------------------------


def test_build_input_items_tool_use_becomes_function_call_item() -> None:
    blocks = [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}]
    result = build_input_items([Message(role="assistant", content=blocks)])
    assert result == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path": "a.py"}',
        }
    ]


def test_build_input_items_tool_result_becomes_function_call_output_item() -> None:
    blocks = [{"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"}]
    result = build_input_items([Message(role="user", content=blocks)])
    assert result == [
        {"type": "function_call_output", "call_id": "call_1", "output": "file contents"}
    ]


def test_build_input_items_tool_result_flattens_list_content() -> None:
    blocks = [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
        }
    ]
    result = build_input_items([Message(role="user", content=blocks)])
    assert result[0]["output"] == "part onepart two"


def test_build_input_items_preserves_interleaved_order() -> None:
    """[text, tool_use, text] on one assistant message -> 3 flat items in order."""
    blocks = [
        {"type": "text", "text": "Let me check that."},
        {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}},
        {"type": "text", "text": "Done."},
    ]
    result = build_input_items([Message(role="assistant", content=blocks)])
    assert len(result) == 3
    assert result[0]["type"] == "message"
    assert result[0]["content"] == [{"type": "input_text", "text": "Let me check that."}]
    assert result[1]["type"] == "function_call"
    assert result[1]["name"] == "read_file"
    assert result[2]["type"] == "message"
    assert result[2]["content"] == [{"type": "input_text", "text": "Done."}]


# ---------------------------------------------------------------------------
# build_input_items -- thinking blocks dropped
# ---------------------------------------------------------------------------


def test_build_input_items_drops_thinking_block() -> None:
    blocks = [{"type": "thinking", "thinking": "reasoning...", "signature": "sig-1"}]
    assert build_input_items([Message(role="assistant", content=blocks)]) == []


def test_build_input_items_drops_unsigned_thinking_block() -> None:
    """A thinking block with no signature (e.g. llama.cpp-origin) is dropped too --

    OpenAI has no item type that accepts a foreign reasoning block back at
    all, regardless of signature.
    """
    blocks = [{"type": "thinking", "thinking": "reasoning..."}]
    assert build_input_items([Message(role="assistant", content=blocks)]) == []


def test_build_input_items_drops_thinking_but_keeps_surrounding_text() -> None:
    blocks = [
        {"type": "thinking", "thinking": "reasoning..."},
        {"type": "text", "text": "Here's my answer."},
    ]
    result = build_input_items([Message(role="assistant", content=blocks)])
    assert len(result) == 1
    assert result[0]["content"] == [{"type": "input_text", "text": "Here's my answer."}]


# ---------------------------------------------------------------------------
# build_input_items -- multiple messages
# ---------------------------------------------------------------------------


def test_build_input_items_multiple_messages_concatenated() -> None:
    messages = [
        Message(role="user", content="turn 1"),
        Message(role="assistant", content="reply 1"),
    ]
    result = build_input_items(messages)
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# build_tool_defs
# ---------------------------------------------------------------------------


def test_build_tool_defs_shape() -> None:
    tool = _make_tool(name="read_file")
    result = build_tool_defs([tool])
    assert len(result) == 1
    entry = result[0]
    assert entry["type"] == "function"
    assert entry["name"] == "read_file"
    assert entry["parameters"] == tool.input_schema
    assert entry["strict"] is False
    assert isinstance(entry["description"], str)
    assert "Description of read_file" in entry["description"]


def test_build_tool_defs_empty_list() -> None:
    assert build_tool_defs([]) == []


def test_build_tool_defs_multiple_tools_preserve_order() -> None:
    tools = [_make_tool(name="tool_a"), _make_tool(name="tool_b")]
    result = build_tool_defs(tools)
    assert [t["name"] for t in result] == ["tool_a", "tool_b"]

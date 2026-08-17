"""Behavior tests for kodo.llms.kimi._convert.

Kimi's OpenAI-compatible endpoint speaks Chat Completions, the same wire
shape as the Google/Alibaba/DeepSeek/local-llamacpp plugins -- so these cases
mirror test_deepseek_convert.py's, except there is no ``thought_signature``-
style replay mechanism to test (see kodo/llms/kimi/_convert.py's module
docstring): a ``thinking`` block is asserted dropped outright, with no
extra_content round-trip case at all.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from kodo.llms._interface import Message, ToolSpec
from kodo.llms.kimi._convert import build_chat_messages, build_openai_tools

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
# build_chat_messages -- system + plain string content
# ---------------------------------------------------------------------------


def test_build_chat_messages_includes_system() -> None:
    result = build_chat_messages("You are helpful.", [])
    assert result == [{"role": "system", "content": "You are helpful."}]


def test_build_chat_messages_string_user_message() -> None:
    result = build_chat_messages("sys", [Message(role="user", content="Hello")])
    assert result[1] == {"role": "user", "content": "Hello"}


def test_build_chat_messages_string_assistant_message() -> None:
    result = build_chat_messages("sys", [Message(role="assistant", content="Hi there")])
    assert result[1] == {"role": "assistant", "content": "Hi there"}


def test_build_chat_messages_strips_callout_from_assistant_string_content() -> None:
    messages = [Message(role="assistant", content="Done. <kodo>All tests pass.</kodo>")]
    result = build_chat_messages("sys", messages)
    assert "<kodo>" not in result[1]["content"]
    assert "Done." in result[1]["content"]


def test_build_chat_messages_does_not_strip_callout_from_user_string_content() -> None:
    messages = [Message(role="user", content="What does <kodo_info>x</kodo_info> mean?")]
    result = build_chat_messages("sys", messages)
    assert result[1]["content"] == "What does <kodo_info>x</kodo_info> mean?"


# ---------------------------------------------------------------------------
# build_chat_messages -- list-of-blocks content: text/tool_use/tool_result
# ---------------------------------------------------------------------------


def test_build_chat_messages_assistant_text_blocks() -> None:
    blocks = [{"type": "text", "text": "hello world"}]
    result = build_chat_messages("sys", [Message(role="assistant", content=blocks)])
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "hello world"


def test_build_chat_messages_assistant_tool_use_becomes_tool_calls() -> None:
    blocks = [{"type": "tool_use", "id": "tool_1", "name": "read_file", "input": {"path": "/x.py"}}]
    result = build_chat_messages("sys", [Message(role="assistant", content=blocks)])
    msg = result[1]
    assert "tool_calls" in msg
    tc = msg["tool_calls"][0]
    assert tc["id"] == "tool_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert tc["function"]["arguments"] == '{"path": "/x.py"}'


def test_build_chat_messages_tool_use_never_carries_extra_content() -> None:
    """Unlike Gemini, no thought_signature (or any) extra_content key is ever added."""
    blocks = [
        {
            "type": "tool_use",
            "id": "tool_1",
            "name": "read_file",
            "input": {"path": "/x.py"},
            "thought_signature": "sig-from-a-different-vendor",
        }
    ]
    result = build_chat_messages("sys", [Message(role="assistant", content=blocks)])
    tc = result[1]["tool_calls"][0]
    assert "extra_content" not in tc


def test_build_chat_messages_user_tool_result_becomes_tool_message() -> None:
    blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}]
    result = build_chat_messages("sys", [Message(role="user", content=blocks)])
    assert result[1] == {"role": "tool", "tool_call_id": "t1", "content": "file contents"}


def test_build_chat_messages_user_tool_result_flattens_list_content() -> None:
    blocks = [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
        }
    ]
    result = build_chat_messages("sys", [Message(role="user", content=blocks)])
    assert result[1]["content"] == "part one part two"


# ---------------------------------------------------------------------------
# build_chat_messages -- thinking blocks dropped, not re-wrapped
# ---------------------------------------------------------------------------


def test_build_chat_messages_drops_thinking_block() -> None:
    blocks = [{"type": "thinking", "thinking": "let me think"}]
    result = build_chat_messages("sys", [Message(role="assistant", content=blocks)])
    assert result[1]["content"] is None
    assert "<think>" not in str(result[1])


def test_build_chat_messages_drops_thinking_but_keeps_surrounding_text() -> None:
    blocks = [
        {"type": "thinking", "thinking": "let me think"},
        {"type": "text", "text": "the answer"},
    ]
    result = build_chat_messages("sys", [Message(role="assistant", content=blocks)])
    assert result[1]["content"] == "the answer"
    assert "let me think" not in result[1]["content"]


# ---------------------------------------------------------------------------
# build_chat_messages -- multiple messages
# ---------------------------------------------------------------------------


def test_build_chat_messages_multiple_messages_concatenated() -> None:
    messages = [
        Message(role="user", content="turn 1"),
        Message(role="assistant", content="reply 1"),
    ]
    result = build_chat_messages("sys", messages)
    assert len(result) == 3  # system + 2 messages
    assert result[1]["role"] == "user"
    assert result[2]["role"] == "assistant"


# ---------------------------------------------------------------------------
# build_openai_tools
# ---------------------------------------------------------------------------


def test_build_openai_tools_shape() -> None:
    tool = _make_tool(name="read_file")
    result = build_openai_tools([tool])
    assert len(result) == 1
    entry = result[0]
    assert entry["type"] == "function"
    assert entry["function"]["name"] == "read_file"
    assert entry["function"]["parameters"] == tool.input_schema
    assert isinstance(entry["function"]["description"], str)
    assert "Description of read_file" in entry["function"]["description"]


def test_build_openai_tools_empty_list() -> None:
    assert build_openai_tools([]) == []


def test_build_openai_tools_multiple_tools_preserve_order() -> None:
    tools = [_make_tool(name="tool_a"), _make_tool(name="tool_b")]
    result = build_openai_tools(tools)
    assert [t["function"]["name"] for t in result] == ["tool_a", "tool_b"]

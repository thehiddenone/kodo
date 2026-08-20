"""Tests for ``kodo.llms.bedrock._convert`` -- kodo history -> Converse shape.

The three Converse constraints that are hard ``ValidationException``s rather
than degraded responses -- no empty content arrays, strictly alternating
roles, tool results under a user message -- are what most of these pin.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from kodo.llms._interface import Message, ToolSpec
from kodo.llms.bedrock._convert import (
    build_converse_messages,
    build_converse_tool_config,
    build_system_blocks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str = "test_tool", **overrides: Any) -> ToolSpec:
    """Mirrors test_openrouter_convert.py's helper of the same name."""
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
# build_system_blocks
# ---------------------------------------------------------------------------


def test_system_blocks_wraps_text() -> None:
    assert build_system_blocks("You are helpful.") == [{"text": "You are helpful."}]


def test_system_blocks_empty_prompt_is_omitted() -> None:
    """Converse rejects a system block whose text is empty."""
    assert build_system_blocks("   ") == []


# ---------------------------------------------------------------------------
# build_converse_messages
# ---------------------------------------------------------------------------


def test_plain_string_messages_round_trip() -> None:
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi there"),
    ]
    assert build_converse_messages(messages) == [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi there"}]},
    ]


def test_system_prompt_is_not_part_of_messages() -> None:
    """Unlike the Chat-Completions vendors, Converse takes system separately."""
    result = build_converse_messages([Message(role="user", content="hello")])
    assert all(m["role"] != "system" for m in result)


def test_tool_use_block_becomes_tool_use_with_object_input() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                {"type": "text", "text": "Reading it."},
                {
                    "type": "tool_use",
                    "id": "tu-1",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                },
            ],
        )
    ]
    content = build_converse_messages(messages)[0]["content"]
    assert content[0] == {"text": "Reading it."}
    # Converse takes a real JSON value here, not the serialized string Chat
    # Completions uses.
    assert content[1] == {
        "toolUse": {"toolUseId": "tu-1", "name": "read_file", "input": {"path": "a.py"}}
    }


def test_tool_use_with_non_dict_input_falls_back_to_empty_object() -> None:
    messages = [
        Message(
            role="assistant",
            content=[{"type": "tool_use", "id": "tu-1", "name": "t", "input": "oops"}],
        )
    ]
    content = build_converse_messages(messages)[0]["content"]
    assert content[0]["toolUse"]["input"] == {}


def test_thinking_blocks_are_dropped() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "visible"},
            ],
        )
    ]
    content = build_converse_messages(messages)[0]["content"]
    assert content == [{"text": "visible"}]


def test_tool_result_becomes_tool_result_under_user_role() -> None:
    messages = [
        Message(
            role="user",
            content=[{"type": "tool_result", "tool_use_id": "tu-1", "content": "file body"}],
        )
    ]
    result = build_converse_messages(messages)
    assert result[0]["role"] == "user"
    assert result[0]["content"] == [
        {"toolResult": {"toolUseId": "tu-1", "content": [{"text": "file body"}]}}
    ]


def test_tool_result_with_nested_block_content_is_flattened() -> None:
    messages = [
        Message(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "tu-1",
                    "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
                }
            ],
        )
    ]
    content = build_converse_messages(messages)[0]["content"]
    assert content[0]["toolResult"]["content"] == [{"text": "one two"}]


def test_empty_messages_are_dropped() -> None:
    """A message that converts to no content blocks would 400 on Converse."""
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content=[{"type": "thinking", "thinking": "only thinking"}]),
        Message(role="user", content="still here"),
    ]
    result = build_converse_messages(messages)
    # The two user turns collapse into one after the empty assistant turn is
    # dropped -- consecutive same-role messages are merged.
    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert result[0]["content"] == [{"text": "hello"}, {"text": "still here"}]


def test_consecutive_same_role_messages_are_merged() -> None:
    """Converse rejects two messages in a row with the same role."""
    messages = [
        Message(role="assistant", content="thinking out loud"),
        Message(
            role="assistant",
            content=[{"type": "tool_use", "id": "tu-1", "name": "t", "input": {}}],
        ),
    ]
    result = build_converse_messages(messages)
    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert len(result[0]["content"]) == 2


def test_roles_alternate_after_conversion() -> None:
    messages = [
        Message(role="user", content="a"),
        Message(role="user", content="b"),
        Message(role="assistant", content="c"),
        Message(role="assistant", content="d"),
        Message(role="user", content="e"),
    ]
    roles = [m["role"] for m in build_converse_messages(messages)]
    assert roles == ["user", "assistant", "user"]


def test_unknown_role_is_treated_as_user() -> None:
    """Converse only knows user/assistant; anything else must not leak through."""
    result = build_converse_messages([Message(role="system", content="stray")])
    assert result[0]["role"] == "user"


# ---------------------------------------------------------------------------
# build_converse_tool_config
# ---------------------------------------------------------------------------


def test_tool_config_empty_when_no_tools() -> None:
    """Converse rejects toolConfig with an empty tools array."""
    assert build_converse_tool_config([]) == {}


def test_tool_config_renders_tool_spec() -> None:
    spec = _make_tool("read_file")
    config = build_converse_tool_config([spec])
    tool = config["tools"][0]["toolSpec"]
    assert tool["name"] == "read_file"
    # Converse nests the JSON Schema one level down, unlike Chat Completions'
    # flat `parameters`.
    assert tool["inputSchema"] == {"json": spec.input_schema}
    assert tool["description"]
